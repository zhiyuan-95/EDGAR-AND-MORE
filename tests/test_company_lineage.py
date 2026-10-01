import sqlite3
from datetime import date
from pathlib import Path

import pytest

from sec_inline_financials.company_lineage import (
    LineageMaintenanceService,
    RegistrantIdentity,
)
from sec_inline_financials.company_purge import CompanyDataPurger
from sec_inline_financials.errors import FilingMetadataError, LineageError
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.migrations import _load_migrations, _statements


def test_existing_company_is_backfilled_as_single_member_lineage(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="New Registrant"),
        purpose="test",
        requested_window={},
    )

    lineage = store.load_company_lineage("2")

    assert lineage is not None
    assert lineage.canonical_current_cik == "0000000002"
    assert lineage.current_to_oldest == (
        RegistrantIdentity(cik="0000000002", legal_name="New Registrant"),
    )
    assert lineage.edges == ()
    with sqlite3.connect(store.database.path) as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_version_six_database_backfills_membership_and_filing_provenance(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    with store.database.write_transaction() as connection:
        for migration in _load_migrations()[:6]:
            for statement in _statements(migration.sql):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
                "VALUES (?, ?, ?, 'now')",
                (migration.version, migration.filename, migration.checksum),
            )
        connection.execute(
            "INSERT INTO companies(id, cik, ticker, current_name, created_at, updated_at) "
            "VALUES (1, '0000000002', 'NEW', 'Legacy Registrant', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO filings(id, company_id, accession, form, filing_date, report_date, "
            "primary_document, source_url) VALUES (1, 1, 'legacy-accession', '10-K', "
            "'2026-02-01', '2025-12-31', 'annual.htm', 'https://www.sec.gov/example')"
        )

    assert store.initialize() == 7
    lineage = store.load_company_lineage("2")

    assert lineage is not None
    assert lineage.current_to_oldest == (
        RegistrantIdentity(cik="0000000002", legal_name="Legacy Registrant"),
    )
    with sqlite3.connect(store.database.path) as connection:
        assert connection.execute(
            "SELECT registrant_cik, archive_owner_cik FROM filing_provenance WHERE filing_id = 1"
        ).fetchone() == ("0000000002", "0000000002")


class _Resolver:
    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        names = {
            "0000000002": "Current Legal Name",
            "0000000001": "Predecessor Legal Name",
            "0000000003": "Oldest Legal Name",
            "0000000004": "Other Company Name",
        }
        return RegistrantIdentity(cik=cik, legal_name=names[cik])


def test_approved_link_creates_explicit_predecessor_edge(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="Stored Current Name"),
        purpose="test",
        requested_window={},
    )
    service = LineageMaintenanceService(store=store, registrant_resolver=_Resolver())

    plan = service.preview_link("2", "1")
    disposition = service.apply_link(plan)

    assert plan.successor.legal_name == "Current Legal Name"
    assert plan.predecessor.legal_name == "Predecessor Legal Name"
    assert disposition == "created"
    assert store.load_company_lineage("2").current_to_oldest == (
        RegistrantIdentity(cik="0000000002", legal_name="Current Legal Name"),
        RegistrantIdentity(cik="0000000001", legal_name="Predecessor Legal Name"),
    )


def test_filing_provenance_is_immutable_for_an_accession(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Current Legal Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(store=store, registrant_resolver=_Resolver())
    service.apply_link(service.preview_link("2", "1"))
    run_id = store.create_processing_run(company, purpose="test", requested_window={})
    predecessor_filing = Filing(
        accession="0000000001-26-000001",
        filing_date=date(2026, 2, 1),
        report_date=date(2025, 12, 31),
        form="10-K",
        primary_document="annual.htm",
        url="https://www.sec.gov/Archives/example/annual.htm",
        registrant_cik="0000000001",
        archive_owner_cik="0000000001",
    )
    store.begin_filing_attempt(run_id, company, predecessor_filing)

    with pytest.raises(FilingMetadataError, match="different metadata"):
        store.begin_filing_attempt(
            run_id,
            company,
            Filing(
                accession=predecessor_filing.accession,
                filing_date=predecessor_filing.filing_date,
                report_date=predecessor_filing.report_date,
                form=predecessor_filing.form,
                primary_document=predecessor_filing.primary_document,
                url=predecessor_filing.url,
                registrant_cik="0000000002",
                archive_owner_cik="0000000002",
            ),
        )


def test_exact_edge_is_idempotent_and_chain_extends_only_from_oldest(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="Current Legal Name"),
        purpose="seed",
        requested_window={},
    )
    service = LineageMaintenanceService(store=store, registrant_resolver=_Resolver())
    service.apply_link(service.preview_link("2", "1"))

    exact_retry = service.preview_link("2", "1")
    assert exact_retry.already_present is True
    assert service.apply_link(exact_retry) == "already_present"
    with pytest.raises(LineageError, match="oldest member"):
        service.preview_link("2", "3")

    service.apply_link(service.preview_link("1", "3"))
    assert [member.cik for member in store.load_company_lineage("2").current_to_oldest] == [
        "0000000002",
        "0000000001",
        "0000000003",
    ]


def test_cross_company_and_stale_preview_conflicts_fail_closed(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="Current Legal Name"),
        purpose="seed",
        requested_window={},
    )
    store.create_processing_run(
        Company(ticker="OTHER", cik="0000000004", name="Other Company Name"),
        purpose="seed",
        requested_window={},
    )
    service = LineageMaintenanceService(store=store, registrant_resolver=_Resolver())

    with pytest.raises(LineageError, match="another stored company"):
        service.preview_link("2", "4")

    first_preview = service.preview_link("2", "1")
    stale_preview = service.preview_link("2", "1")
    service.apply_link(first_preview)
    with pytest.raises(FilingMetadataError, match="changed after preview"):
        service.apply_link(stale_preview)


def test_company_purge_removes_its_lineage_without_touching_other_company(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    first_run = store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="Current Legal Name"),
        purpose="seed",
        requested_window={},
    )
    other_run = store.create_processing_run(
        Company(ticker="OTHER", cik="0000000004", name="Other Company Name"),
        purpose="seed",
        requested_window={},
    )
    store.finish_processing_run(first_run)
    store.finish_processing_run(other_run)
    service = LineageMaintenanceService(store=store, registrant_resolver=_Resolver())
    service.apply_link(service.preview_link("2", "1"))

    CompanyDataPurger(store).purge(("NEW",))

    assert store.load_company_lineage("2") is None
    assert store.load_company_lineage("4") is not None
