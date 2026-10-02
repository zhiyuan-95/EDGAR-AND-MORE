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


@dataclass(frozen=True)
class MigrationStatus:
    database_exists: bool
    current_version: int
    supported_version: int
    pending: tuple[Migration, ...]

    @property
    def newer_than_code(self) -> bool:
        return self.current_version > self.supported_version

    @property
    def is_current(self) -> bool:
        return not self.newer_than_code and not self.pending

    @property
    def pending_filenames(self) -> tuple[str, ...]:
        return tuple(migration.filename for migration in self.pending)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_line_endings(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def _load_migrations() -> tuple[Migration, ...]:
    package = resources.files("sec_inline_financials.storage.sql")
    migrations: list[Migration] = []
    for resource in sorted(package.iterdir(), key=lambda item: item.name):
        if not resource.name.endswith(".sql"):
            continue
        version_text, separator, _name = resource.name.partition("_")
        if not separator or not version_text.isdigit():
            raise SchemaError(f"Invalid migration filename: {resource.name}")
        raw = _normalize_line_endings(resource.read_bytes())
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


def _read_applied_migrations(
    connection: sqlite3.Connection,
) -> dict[int, tuple[str, str]]:
    ledger_exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_migrations'"
    ).fetchone()

    if ledger_exists is None:
        return {}

    return {
        int(row["version"]): (str(row["filename"]), str(row["checksum"]))
        for row in connection.execute(
            "SELECT version, filename, checksum FROM schema_migrations ORDER BY version"
        )
    }


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


def inspect_database_schema(database: EvidenceDatabase) -> MigrationStatus:
    """Report migration state without creating or modifying the database."""
    migrations = _load_migrations()
    supported_version = migrations[-1].version

    if not database.path.exists():
        return MigrationStatus(
            database_exists=False,
            current_version=0,
            supported_version=supported_version,
            pending=migrations,
        )

    try:
        with database.read_connection() as connection:
            applied = _read_applied_migrations(connection)
    except sqlite3.DatabaseError as exc:
        raise SchemaError(f"Could not inspect evidence database: {exc}") from exc

    current_version = max(applied, default=0)

    # Continue validating every packaged migration that appears in the ledger.
    for migration in migrations:
        recorded = applied.get(migration.version)
        if recorded is not None and recorded != (
            migration.filename,
            migration.checksum,
        ):
            raise SchemaError(
                f"Applied migration {migration.version} does not match "
                f"packaged {migration.filename}."
            )

    pending = tuple(migration for migration in migrations if migration.version not in applied)

    return MigrationStatus(
        database_exists=True,
        current_version=current_version,
        supported_version=supported_version,
        pending=pending,
    )


def _require_database_integrity(connection: sqlite3.Connection) -> None:
    foreign_key_issues = connection.execute("PRAGMA foreign_key_check").fetchall()
    integrity_messages = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check"))

    failures: list[str] = []

    if foreign_key_issues:
        samples = ", ".join(
            f"{row['table']} rowid={row['rowid']} -> {row['parent']}"
            for row in foreign_key_issues[:5]
        )
        failures.append(
            f"foreign_key_check reported {len(foreign_key_issues)} violation(s): {samples}"
        )

    if integrity_messages != ("ok",):
        details = "; ".join(integrity_messages[:5]) or "no result"
        failures.append(f"integrity_check reported: {details}")

    if failures:
        raise SchemaError("Migration integrity validation failed: " + "; ".join(failures))


def initialize_database(database: EvidenceDatabase) -> int:
    migrations = _load_migrations()
    try:
        with database.write_transaction() as connection:
            applied = _read_applied_migrations(connection)
            newest_known = migrations[-1].version
            if applied and max(applied) > newest_known:
                raise SchemaError(
                    f"Database schema version {max(applied)} is newer than supported "
                    f"version {newest_known}."
                )
            pending_migrations: list[Migration] = []

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

                pending_migrations.append(migration)

            if pending_migrations:
                _require_database_integrity(connection)

                for migration in pending_migrations:
                    connection.execute(
                        "INSERT INTO schema_migrations("
                        "version, filename, checksum, applied_at"
                        ") VALUES (?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.filename,
                            migration.checksum,
                            _utc_now(),
                        ),
                    )
            _require_database_integrity(connection)
        return migrations[-1].version
    except SchemaError:
        raise
    except sqlite3.DatabaseError as exc:
        raise SchemaError(f"Could not initialize evidence database: {exc}") from exc
