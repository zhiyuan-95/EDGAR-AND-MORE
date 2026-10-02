from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from sec_inline_financials.errors import SchemaError
from sec_inline_financials.storage.database import EvidenceDatabase
from sec_inline_financials.storage.migrations import (
    initialize_database,
    inspect_database_schema,
)

DatabaseAtVersionFactory = Callable[[int], Path]


def _assert_migration_rolled_back(database_path: Path) -> None:
    status = inspect_database_schema(EvidenceDatabase(database_path))

    assert status.current_version == 6
    assert status.pending_filenames == (
        "0007_cik_lineage.sql",
        "0008_reporting_transition_dates.sql",
    )


def test_migration_rolls_back_when_foreign_key_check_fails(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(6)

    # Introduce a pre-existing orphan that foreign_keys=ON alone will not detect.
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.executescript(
            """
            CREATE TABLE probe_parent (
                id INTEGER PRIMARY KEY
            );

            CREATE TABLE probe_child (
                parent_id INTEGER REFERENCES probe_parent(id)
            );

            INSERT INTO probe_child(parent_id) VALUES (999);
            """
        )

    with pytest.raises(
        SchemaError,
        match="foreign_key_check reported 1 violation",
    ):
        initialize_database(EvidenceDatabase(database_path))

    _assert_migration_rolled_back(database_path)


def test_migration_rolls_back_when_integrity_check_fails(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(6)

    # Create a CHECK-constraint violation that integrity_check will detect.
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA ignore_check_constraints = ON")
        connection.execute("CREATE TABLE integrity_probe(value INTEGER CHECK (value > 0))")
        connection.execute("INSERT INTO integrity_probe(value) VALUES (0)")

    with pytest.raises(
        SchemaError,
        match="integrity_check reported",
    ):
        initialize_database(EvidenceDatabase(database_path))

    _assert_migration_rolled_back(database_path)
