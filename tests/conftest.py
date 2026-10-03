from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable
from importlib import resources
from pathlib import Path

import pytest

DatabaseAtVersionFactory = Callable[[int], Path]


@pytest.fixture
def database_at_version(tmp_path: Path) -> DatabaseAtVersionFactory:
    """Create a real evidence database with migrations applied through a version."""

    def create(version: int) -> Path:
        runtime_root = tmp_path / f"runtime-v{version}"
        runtime_root.mkdir()
        database_path = runtime_root / "evidence.sqlite3"
        package = resources.files("sec_inline_financials.storage.sql")

        with sqlite3.connect(database_path) as connection:
            for resource in sorted(package.iterdir(), key=lambda item: item.name):
                if not resource.name.endswith(".sql"):
                    continue
                version_text, separator, _name = resource.name.partition("_")
                if not separator or not version_text.isdigit():
                    continue
                migration_version = int(version_text)
                if migration_version > version:
                    break

                raw = resource.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                connection.executescript(raw.decode("utf-8"))
                connection.execute(
                    "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        migration_version,
                        resource.name,
                        hashlib.sha256(raw).hexdigest(),
                        "2026-01-01T00:00:00+00:00",
                    ),
                )

        return database_path

    return create
