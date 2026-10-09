from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from sec_inline_financials.storage.database import EvidenceDatabase
from sec_inline_financials.storage.migrations import inspect_database_schema

DatabaseAtVersionFactory = Callable[[int], Path]


def _run_cli_module(
    module: str,
    runtime_root: Path,
    answers: str,
    *arguments: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["SEC_INLINE_FINANCIALS_DATA_DIR"] = str(runtime_root)
    environment["SEC_USER_AGENT"] = "CLI Test test@example.com"
    return subprocess.run(
        [sys.executable, "-m", module, *arguments],
        input=answers,
        text=True,
        capture_output=True,
        check=False,
        env=environment,
    )


def _run_lineage_cli(runtime_root: Path, answers: str) -> subprocess.CompletedProcess[str]:
    return _run_cli_module(
        "sec_inline_financials.lineage_cli",
        runtime_root,
        answers,
        "0000000001",
        "0000000002",
    )


def _run_lineage_change_cli(
    runtime_root: Path,
    answers: str,
) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["SEC_INLINE_FINANCIALS_DATA_DIR"] = str(runtime_root)
    environment["SEC_USER_AGENT"] = "CLI Test test@example.com"
    return subprocess.run(
        [
            sys.executable,
            "-c",
            "from sec_inline_financials.lineage_cli import promotion_main; "
            "raise SystemExit(promotion_main())",
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
        "0009_company_scoped_filing_provenance.sql",
    )


def test_lineage_change_declining_migration_preserves_v6_database(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(6)
    before = database_path.read_bytes()

    result = _run_lineage_change_cli(database_path.parent, "n\n")

    assert result.returncode == 0
    assert "Apply these database migrations? [y/n]:" in result.stdout
    assert "Migration cancelled; database unchanged." in result.stdout
    assert database_path.read_bytes() == before


def test_lineage_change_approves_migration_before_promotion_validation(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(6)

    result = _run_lineage_change_cli(database_path.parent, "y\n")

    assert result.returncode == 1
    assert "Apply these database migrations? [y/n]:" in result.stdout
    assert "Database upgraded to version 9." in result.stdout
    assert "does not belong to a stored company lineage" in result.stdout
    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 9
    assert status.is_current


def test_lineage_change_current_database_skips_migration_approval(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)

    result = _run_lineage_change_cli(database_path.parent, "")

    assert result.returncode == 1
    assert "Apply these database migrations?" not in result.stdout
    assert "does not belong to a stored company lineage" in result.stdout


def test_lineage_change_rejects_newer_database_without_mutation(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (10, "0010_future.sql", "future-checksum", "2026-01-02T00:00:00+00:00"),
        )
    before = database_path.read_bytes()

    result = _run_lineage_change_cli(database_path.parent, "")

    assert result.returncode == 1
    assert "Database schema version 10 is newer than supported version 9." in result.stdout
    assert "Apply these database migrations?" not in result.stdout
    assert database_path.read_bytes() == before


@pytest.mark.parametrize(
    ("module", "arguments"),
    (
        ("sec_inline_financials.company_ingestion", ("AAPL",)),
        ("sec_inline_financials.mapping_cli", ("AAPL",)),
        ("sec_inline_financials.company_purge_cli", ("AAPL",)),
    ),
)
def test_declining_migration_preserves_v6_database_for_ordinary_commands(
    database_at_version: DatabaseAtVersionFactory,
    module: str,
    arguments: tuple[str, ...],
) -> None:
    database_path = database_at_version(6)
    before = database_path.read_bytes()

    result = _run_cli_module(module, database_path.parent, "n\n", *arguments)

    assert result.returncode == 0
    assert "Apply these database migrations? [y/n]:" in result.stdout
    assert "Migration cancelled; database unchanged." in result.stdout
    assert database_path.read_bytes() == before

    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 6
    assert status.pending_filenames == (
        "0007_cik_lineage.sql",
        "0008_reporting_transition_dates.sql",
        "0009_company_scoped_filing_provenance.sql",
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
    assert "Database upgraded to version 9." in result.stdout
    assert "does not belong to a stored company lineage" in result.stdout

    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 9
    assert status.is_current


def test_newer_database_is_rejected_without_mutation(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (10, "0010_future.sql", "future-checksum", "2026-01-02T00:00:00+00:00"),
        )
    before = database_path.read_bytes()

    result = _run_lineage_cli(database_path.parent, "")

    assert result.returncode == 1
    assert "Database schema version 10 is newer than supported version 9." in result.stdout
    assert "Apply these database migrations?" not in result.stdout
    assert database_path.read_bytes() == before


def test_current_database_skips_migration_approval(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)

    result = _run_lineage_cli(database_path.parent, "")

    assert result.returncode == 1
    assert "Apply these database migrations?" not in result.stdout
    assert "does not belong to a stored company lineage" in result.stdout

    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 9
    assert status.is_current
