from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from sec_inline_financials.errors import SchemaError
from sec_inline_financials.storage.database import EvidenceDatabase
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.migrations import (
    _load_migrations,
    initialize_database,
    inspect_database_schema,
)

DatabaseAtVersionFactory = Callable[[int], Path]
_MIGRATION_8_CHECKSUM = "6afda0f24257177c85097efc7d2d4c8b89eac1ae876fbc32caa966be3e8d74cd"
_MIGRATION_8_SQL = "ALTER TABLE company_cik_transitions ADD COLUMN effective_date TEXT;\n"


def _assert_migration_rolled_back(database_path: Path) -> None:
    status = inspect_database_schema(EvidenceDatabase(database_path))

    assert status.current_version == 6
    assert status.pending_filenames == (
        "0007_cik_lineage.sql",
        "0008_reporting_transition_dates.sql",
        "0009_company_scoped_filing_provenance.sql",
    )


def _insert_company(connection: sqlite3.Connection, *, cik: str, ticker: str, name: str) -> int:
    now = "2026-01-01T00:00:00+00:00"
    cursor = connection.execute(
        "INSERT INTO companies(cik, ticker, current_name, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (cik, ticker, name, now, now),
    )
    company_id = int(cursor.lastrowid)
    connection.execute(
        "INSERT INTO company_ciks(company_id, cik, legal_name, associated_at, updated_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (company_id, cik, name, now, now),
    )
    return company_id


def _insert_filing(connection: sqlite3.Connection, *, company_id: int, accession: str) -> int:
    cursor = connection.execute(
        "INSERT INTO filings(company_id, accession, form, filing_date, report_date, "
        "primary_document, source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            company_id,
            accession,
            "10-K",
            "2026-01-02",
            "2025-12-31",
            "index.htm",
            "https://www.sec.gov/Archives/index.htm",
        ),
    )
    return int(cursor.lastrowid)


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


def test_migration_rejects_cross_company_filing_provenance(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(8)

    with sqlite3.connect(database_path) as connection:
        successor_id = _insert_company(
            connection,
            cik="0000000001",
            ticker="ONE",
            name="One Corp",
        )
        other_id = _insert_company(
            connection,
            cik="0000000002",
            ticker="TWO",
            name="Two Corp",
        )
        filing_id = _insert_filing(
            connection,
            company_id=successor_id,
            accession="0000000001-26-000001",
        )
        assert successor_id != other_id
        connection.execute(
            "INSERT INTO filing_provenance("
            "filing_id, registrant_cik, archive_owner_cik, recorded_at"
            ") VALUES (?, ?, ?, ?)",
            (filing_id, "0000000002", "0000000002", "2026-01-01T00:00:00+00:00"),
        )

    with pytest.raises(
        SchemaError,
        match="filing provenance CIKs must belong to the filing company",
    ):
        initialize_database(EvidenceDatabase(database_path))

    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.current_version == 8
    assert status.pending_filenames == ("0009_company_scoped_filing_provenance.sql",)


def test_migration_8_remains_compatible_with_existing_database(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(7)

    with sqlite3.connect(database_path) as connection:
        connection.executescript(_MIGRATION_8_SQL)
        connection.execute(
            "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
            "VALUES (?, ?, ?, ?)",
            (
                8,
                "0008_reporting_transition_dates.sql",
                _MIGRATION_8_CHECKSUM,
                "2026-01-01T00:00:00+00:00",
            ),
        )

    status = inspect_database_schema(EvidenceDatabase(database_path))

    assert status.current_version == 8
    assert status.pending_filenames == ("0009_company_scoped_filing_provenance.sql",)


def test_filing_provenance_rejects_cross_company_direct_sql(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)

    with sqlite3.connect(database_path) as connection:
        successor_id = _insert_company(
            connection,
            cik="0000000001",
            ticker="ONE",
            name="One Corp",
        )
        other_id = _insert_company(
            connection,
            cik="0000000002",
            ticker="TWO",
            name="Two Corp",
        )
        filing_id = _insert_filing(
            connection,
            company_id=successor_id,
            accession="0000000001-26-000001",
        )
        assert successor_id != other_id

        with pytest.raises(
            sqlite3.IntegrityError,
            match="filing provenance CIKs must belong to the filing company",
        ):
            connection.execute(
                "INSERT INTO filing_provenance("
                "filing_id, company_id, registrant_cik, archive_owner_cik, recorded_at"
                ") VALUES (?, ?, ?, ?, ?)",
                (
                    filing_id,
                    successor_id,
                    "0000000002",
                    "0000000002",
                    "2026-01-01T00:00:00+00:00",
                ),
            )

        connection.execute(
            "INSERT INTO filing_provenance("
            "filing_id, company_id, registrant_cik, archive_owner_cik, recorded_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                filing_id,
                successor_id,
                "0000000001",
                "0000000001",
                "2026-01-01T00:00:00+00:00",
            ),
        )


def test_latest_migration_adds_transition_effective_date_index(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)

    with sqlite3.connect(database_path) as connection:
        indexes = {
            str(row[1]) for row in connection.execute("PRAGMA index_list(company_cik_transitions)")
        }

    assert "company_cik_transitions_successor_effective_idx" in indexes


def test_store_initialize_requires_explicit_approval_for_existing_upgrade(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(6)
    store = EvidenceStore(database_path, database_path.parent)

    with pytest.raises(
        SchemaError,
        match="Existing evidence databases require explicit migration approval",
    ):
        store.initialize()

    _assert_migration_rolled_back(database_path)

    assert store.initialize(allow_schema_upgrade=True) == _load_migrations()[-1].version
    status = inspect_database_schema(EvidenceDatabase(database_path))
    assert status.is_current


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
