from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from sec_inline_financials.storage.database import EvidenceDatabase
from sec_inline_financials.storage.migrations import inspect_database_schema

DatabaseAtVersionFactory = Callable[[int], Path]


def _run_lineage_cli(runtime_root: Path, answers: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["SEC_INLINE_FINANCIALS_DATA_DIR"] = str(runtime_root)
    environment["SEC_USER_AGENT"] = "CLI Test test@example.com"
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "sec_inline_financials.lineage_cli",
            "0000000001",
            "0000000002",
        ],
        input=answers,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def test_declining_migration_preserves_v6_database(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(6)
    before = database_path.read_bytes()

    result = _run_lineage_cli(database_path.parent, "n\n")

    assert result.returncode == 0
    assert "Apply these database migrations? [y/n]:" in result.stdout
    assert "Migration cancelled; database unchanged." in result.stdout
    assert database_path.read_bytes() == before

    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 6
    assert status.pending_filenames == (
        "0007_cik_lineage.sql",
        "0008_reporting_transition_dates.sql",
    )


def test_approving_migration_upgrades_before_lineage_validation(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(6)

    result = _run_lineage_cli(database_path.parent, "y\n")

    # The empty fixture has no stored successor company, so lineage validation
    # stops after the separately approved migration completes.
    assert result.returncode == 1
    assert "Apply these database migrations? [y/n]:" in result.stdout
    assert "Database upgraded to version 8." in result.stdout
    assert "does not belong to a stored company lineage" in result.stdout

    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 8
    assert status.is_current


def test_newer_database_is_rejected_without_mutation(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(8)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (9, "0009_future.sql", "future-checksum", "2026-01-02T00:00:00+00:00"),
        )
    before = database_path.read_bytes()

    result = _run_lineage_cli(database_path.parent, "")

    assert result.returncode == 1
    assert "Database schema version 9 is newer than supported version 8." in result.stdout
    assert "Apply these database migrations?" not in result.stdout
    assert database_path.read_bytes() == before


def test_current_database_skips_migration_approval(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(8)

    result = _run_lineage_cli(database_path.parent, "")

    assert result.returncode == 1
    assert "Apply these database migrations?" not in result.stdout
    assert "does not belong to a stored company lineage" in result.stdout

    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 8
    assert status.is_current
