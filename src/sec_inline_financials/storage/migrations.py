from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from importlib import resources

from sec_inline_financials.errors import SchemaError
from sec_inline_financials.storage.database import EvidenceDatabase


@dataclass(frozen=True)
class Migration:
    version: int
    filename: str
    checksum: str
    sql: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_migrations() -> tuple[Migration, ...]:
    package = resources.files("sec_inline_financials.storage.sql")
    migrations: list[Migration] = []
    for resource in sorted(package.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".sql"):
            continue
        version_text, separator, _name = resource.name.partition("_")
        if not separator or not version_text.isdigit():
            raise SchemaError(f"Invalid migration filename: {resource.name}")
        raw = resource.read_bytes()
        migrations.append(
            Migration(
                version=int(version_text),
                filename=resource.name,
                checksum=hashlib.sha256(raw).hexdigest(),
                sql=raw.decode("utf-8"),
            )
        )
    if not migrations:
        raise SchemaError("No packaged evidence database migrations were found.")
    versions = [migration.version for migration in migrations]
    if versions != list(range(1, len(versions) + 1)):
        raise SchemaError(f"Migration versions must be consecutive from 1; found {versions}.")
    return tuple(migrations)


def _statements(sql: str) -> tuple[str, ...]:
    statements: list[str] = []
    pending = ""
    for line in sql.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statement = pending.strip()
            if statement:
                statements.append(statement)
            pending = ""
    if pending.strip():
        raise SchemaError("Migration ends with an incomplete SQL statement.")
    return tuple(statements)


def initialize_database(database: EvidenceDatabase) -> int:
    migrations = _load_migrations()
    try:
        with database.write_transaction() as connection:
            ledger_exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
            ).fetchone()
            applied: dict[int, tuple[str, str]] = {}
            if ledger_exists:
                applied = {
                    int(row["version"]): (str(row["filename"]), str(row["checksum"]))
                    for row in connection.execute(
                        "SELECT version, filename, checksum FROM schema_migrations ORDER BY version"
                    )
                }
            newest_known = migrations[-1].version
            if applied and max(applied) > newest_known:
                raise SchemaError(
                    f"Database schema version {max(applied)} is newer than supported "
                    f"version {newest_known}."
                )
            for migration in migrations:
                recorded = applied.get(migration.version)
                if recorded is not None:
                    if recorded != (migration.filename, migration.checksum):
                        raise SchemaError(
                            f"Applied migration {migration.version} does not match packaged "
                            f"{migration.filename}."
                        )
                    continue
                for statement in _statements(migration.sql):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        migration.version,
                        migration.filename,
                        migration.checksum,
                        _utc_now(),
                    ),
                )
        return migrations[-1].version
    except SchemaError:
        raise
    except sqlite3.DatabaseError as exc:
        raise SchemaError(f"Could not initialize evidence database: {exc}") from exc
