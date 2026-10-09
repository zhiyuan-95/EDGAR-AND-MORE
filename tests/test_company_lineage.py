from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date
from pathlib import Path

import pytest

from sec_inline_financials.company_ingestion import CompanyIngestionService
from sec_inline_financials.company_lineage import (
    LineageError,
    LineageMaintenanceService,
    RegistrantIdentity,
    normalize_cik,
)
from sec_inline_financials.errors import FilingMetadataError
from sec_inline_financials.evidence_models import (
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.sec_client import DiscoveredCompanyWindow
from sec_inline_financials.storage.evidence_store import EvidenceStore

DatabaseAtVersionFactory = Callable[[int], Path]


@pytest.mark.parametrize(
    ("raw", "expected"),
    (("1", "0000000001"), (" 1234567890 ", "1234567890"), ("0000000042", "0000000042")),
)
def test_normalize_cik_accepts_only_ascii_decimal_ciks(raw: str, expected: str) -> None:
    assert normalize_cik(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ("", "0", "-1", "12345678901", "١٢٣", "１２３", "²"),
)
def test_normalize_cik_rejects_invalid_or_non_ascii_digits(raw: str) -> None:
    with pytest.raises(LineageError, match="positive number"):
        normalize_cik(raw)


class StaticRegistrantResolver:
    def __init__(self, identities: dict[str, str]) -> None:
        self._identities = identities

    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        return RegistrantIdentity(cik=cik, legal_name=self._identities[cik])


class _EmptyEvidenceProcessor:
    def __init__(self) -> None:
        self._profile = ExtractionProfile(
            application_version="test",
            extractor_version="test",
            arelle_version="test",
            validation_options=(),
            transform_plugin_revision="test",
            transform_plugin_hashes=(),
            serialization_version="evidence-v3",
        )

    def extraction_profile(self) -> ExtractionProfile:
        return self._profile

    def extract_evidence(
        self,
        company: Company,
        filing: Filing,
        _capture_area: Path,
    ) -> FilingEvidenceBundle:
        return FilingEvidenceBundle(
            company=company,
            filing=filing,
            captured_company_name=company.name,
            captured_company_ticker=company.ticker,
            fiscal_year=filing.report_date.year,
            fiscal_period="FY",
            fiscal_year_source="test",
            fiscal_period_source="test",
            source_documents=(),
            concepts=(),
            concept_labels=(),
            contexts=(),
            units=(),
            observations=(),
            diagnostics=(),
            validation_messages=(),
            calculation_networks=(),
            calculation_relationships=(),
            extraction_profile=self._profile,
            coverage_manifest=CoverageManifest(
                recognized_fact_count=0,
                unresolved_observation_count=0,
                numeric_count=0,
                nonnumeric_count=0,
                nil_count=0,
                invalid_count=0,
                context_count=0,
                unit_count=0,
                validation_message_count=0,
                calculation_relationship_count=0,
                source_document_count=0,
            ),
            raw_log_json="{}",
        )


def _insert_company(
    database_path: Path,
    *,
    cik: str,
    name: str,
    ticker: str = "TEST",
) -> None:
    with sqlite3.connect(database_path) as connection:
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


def test_apply_and_ingest_without_ingestion_callback_does_not_commit_lineage(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    successor_cik = "0000000001"
    predecessor_cik = "0000000002"
    _insert_company(database_path, cik=successor_cik, name="Successor Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                successor_cik: "Successor Corp",
                predecessor_cik: "Predecessor Corp",
            }
        ),
    )

    plan = service.preview_link(successor_cik, predecessor_cik)

    with pytest.raises(LineageError, match="Lineage ingestion is not configured"):
        service.apply_and_ingest(plan)

    with sqlite3.connect(database_path) as connection:
        predecessor_count = connection.execute(
            "SELECT count(*) FROM company_ciks WHERE cik = ?",
            (predecessor_cik,),
        ).fetchone()[0]
        transition_count = connection.execute(
            "SELECT count(*) FROM company_cik_transitions",
        ).fetchone()[0]

    assert predecessor_count == 0
    assert transition_count == 0


def test_successor_promotion_preview_extends_the_canonical_end_of_the_lineage(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    service.apply_link(service.preview_link(current_cik, older_cik))

    plan = service.preview_successor_promotion(current_cik, new_successor_cik)

    assert plan.before.canonical_current_cik == current_cik
    assert plan.after.canonical_current_cik == new_successor_cik
    assert tuple(member.cik for member in plan.after.current_to_oldest) == (
        new_successor_cik,
        current_cik,
        older_cik,
    )
    assert tuple((edge.predecessor_cik, edge.successor_cik) for edge in plan.after.edges) == (
        (older_cik, current_cik),
        (current_cik, new_successor_cik),
    )
    assert plan.predecessor.cik == current_cik
    assert plan.successor.cik == new_successor_cik
    assert not plan.already_present


def test_successor_promotion_updates_the_existing_company_and_preserves_its_ticker(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    service.apply_link(service.preview_link(current_cik, older_cik))
    with sqlite3.connect(database_path) as connection:
        original = connection.execute(
            "SELECT id, ticker FROM companies WHERE cik = ?",
            (current_cik,),
        ).fetchone()
    assert original is not None

    disposition = store.apply_successor_promotion(
        service.preview_successor_promotion(current_cik, new_successor_cik)
    )

    assert disposition == "created"
    lineage = store.load_company_lineage(new_successor_cik)
    assert lineage is not None
    assert lineage.canonical_current_cik == new_successor_cik
    assert tuple(member.cik for member in lineage.current_to_oldest) == (
        new_successor_cik,
        current_cik,
        older_cik,
    )
    with sqlite3.connect(database_path) as connection:
        promoted = connection.execute(
            "SELECT id, cik, ticker, current_name FROM companies WHERE id = ?",
            (original[0],),
        ).fetchone()
    assert promoted == (
        original[0],
        new_successor_cik,
        original[1],
        "New Successor Corp",
    )


def test_successor_promotion_rerun_is_idempotent(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    service.apply_link(service.preview_link(current_cik, older_cik))
    store.apply_successor_promotion(
        service.preview_successor_promotion(current_cik, new_successor_cik)
    )

    rerun = service.preview_successor_promotion(current_cik, new_successor_cik)
    disposition = store.apply_successor_promotion(rerun)

    assert rerun.already_present
    assert rerun.before.canonical_current_cik == new_successor_cik
    assert disposition == "already_present"
    with sqlite3.connect(database_path) as connection:
        member_count = connection.execute(
            "SELECT count(*) FROM company_ciks WHERE company_id = ?",
            (rerun.company_id,),
        ).fetchone()[0]
        transition_count = connection.execute(
            "SELECT count(*) FROM company_cik_transitions WHERE company_id = ?",
            (rerun.company_id,),
        ).fetchone()[0]
    assert member_count == 3
    assert transition_count == 2


def test_successor_promotion_commits_before_ingesting_the_new_canonical_cik(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    ingested: list[str] = []

    def ingest(cik: str) -> str:
        lineage = store.load_company_lineage(cik)
        assert lineage is not None
        assert lineage.canonical_current_cik == new_successor_cik
        ingested.append(cik)
        return "ingested"

    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
        ingest_company_cik=ingest,
    )

    result = service.apply_successor_and_ingest(
        service.preview_successor_promotion(current_cik, new_successor_cik)
    )

    assert result.disposition == "created"
    assert result.ingestion == "ingested"
    assert ingested == [new_successor_cik]


def test_successor_promotion_preserves_existing_filing_and_provenance_rows(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    service.apply_link(service.preview_link(current_cik, older_cik))
    with sqlite3.connect(database_path) as connection:
        company_id = connection.execute(
            "SELECT id FROM companies WHERE cik = ?",
            (current_cik,),
        ).fetchone()[0]
        cursor = connection.execute(
            "INSERT INTO filings(company_id, accession, form, filing_date, report_date, "
            "primary_document, source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                company_id,
                "0000000001-25-000001",
                "10-K",
                "2026-02-01",
                "2025-12-31",
                "older.htm",
                "https://www.sec.gov/Archives/older.htm",
            ),
        )
        filing_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO filing_provenance("
            "filing_id, company_id, registrant_cik, archive_owner_cik, recorded_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                filing_id,
                company_id,
                older_cik,
                older_cik,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        before_filing = connection.execute(
            "SELECT * FROM filings WHERE id = ?", (filing_id,)
        ).fetchone()
        before_provenance = connection.execute(
            "SELECT * FROM filing_provenance WHERE filing_id = ?", (filing_id,)
        ).fetchone()

    store.apply_successor_promotion(
        service.preview_successor_promotion(current_cik, new_successor_cik)
    )

    with sqlite3.connect(database_path) as connection:
        after_filing = connection.execute(
            "SELECT * FROM filings WHERE id = ?", (filing_id,)
        ).fetchone()
        after_provenance = connection.execute(
            "SELECT * FROM filing_provenance WHERE filing_id = ?", (filing_id,)
        ).fetchone()
        foreign_key_violations = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert after_filing == before_filing
    assert after_provenance == before_provenance
    assert foreign_key_violations == []


def test_stale_successor_promotion_preview_is_rejected_without_mutation(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    stale_plan = service.preview_successor_promotion(current_cik, new_successor_cik)
    service.apply_link(service.preview_link(current_cik, older_cik))

    with pytest.raises(FilingMetadataError, match="changed after preview"):
        store.apply_successor_promotion(stale_plan)

    lineage = store.load_company_lineage(current_cik)
    assert lineage is not None
    assert lineage.canonical_current_cik == current_cik
    assert store.load_company_lineage(new_successor_cik) is None


def test_successor_promotion_transaction_failure_rolls_back_every_lineage_change(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    plan = service.preview_successor_promotion(current_cik, new_successor_cik)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TRIGGER fail_successor_promotion "
            "BEFORE UPDATE OF cik ON companies "
            "BEGIN SELECT RAISE(ABORT, 'injected promotion failure'); END"
        )

    with pytest.raises(sqlite3.IntegrityError, match="injected promotion failure"):
        store.apply_successor_promotion(plan)

    lineage = store.load_company_lineage(current_cik)
    assert lineage is not None
    assert lineage.canonical_current_cik == current_cik
    assert store.load_company_lineage(new_successor_cik) is None
    with sqlite3.connect(database_path) as connection:
        member_count = connection.execute(
            "SELECT count(*) FROM company_ciks WHERE company_id = ?", (plan.company_id,)
        ).fetchone()[0]
        transition_count = connection.execute(
            "SELECT count(*) FROM company_cik_transitions WHERE company_id = ?",
            (plan.company_id,),
        ).fetchone()[0]
    assert member_count == 1
    assert transition_count == 0


def test_successor_promotion_rerun_fills_date_and_refreshes_endpoint_names(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    new_successor_cik = "0000000003"
    names = {
        current_cik: "Current Corp",
        new_successor_cik: "New Successor Corp",
    }
    _insert_company(database_path, cik=current_cik, name="Stored Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(names),
    )
    store.apply_successor_promotion(
        service.preview_successor_promotion(current_cik, new_successor_cik)
    )
    names[current_cik] = "Former Current Corp"
    names[new_successor_cik] = "Renamed Successor Corp"

    plan = service.preview_successor_promotion(
        current_cik,
        new_successor_cik,
        effective_date=date(2026, 5, 1),
    )
    disposition = store.apply_successor_promotion(plan)

    assert disposition == "updated"
    lineage = store.load_company_lineage(new_successor_cik)
    assert lineage is not None
    assert tuple((member.cik, member.legal_name) for member in lineage.current_to_oldest) == (
        (new_successor_cik, "Renamed Successor Corp"),
        (current_cik, "Former Current Corp"),
    )
    assert lineage.edges[0].effective_date == date(2026, 5, 1)


def test_successor_promotion_name_only_rerun_refreshes_names_without_changing_state_hash(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    original = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                new_successor_cik: "Successor Corp",
            }
        ),
    )
    store.apply_successor_promotion(
        original.preview_successor_promotion(current_cik, new_successor_cik)
    )
    before = store.load_company_lineage(new_successor_cik)
    assert before is not None
    refreshed = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Former Current Corp",
                new_successor_cik: "Renamed Successor Corp",
            }
        ),
    )

    plan = refreshed.preview_successor_promotion(current_cik, new_successor_cik)
    disposition = store.apply_successor_promotion(plan)

    after = store.load_company_lineage(new_successor_cik)
    assert after is not None
    assert plan.already_present
    assert disposition == "already_present"
    assert after.state_hash == before.state_hash
    assert tuple((member.cik, member.legal_name) for member in after.current_to_oldest) == (
        (new_successor_cik, "Renamed Successor Corp"),
        (current_cik, "Former Current Corp"),
    )
    with sqlite3.connect(database_path) as connection:
        assert connection.execute(
            "SELECT current_name FROM companies WHERE id = ?", (after.company_id,)
        ).fetchone() == ("Renamed Successor Corp",)


def test_successor_promotion_without_ingestion_callback_does_not_commit(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    plan = service.preview_successor_promotion(current_cik, new_successor_cik)

    with pytest.raises(LineageError, match="Lineage ingestion is not configured"):
        service.apply_successor_and_ingest(plan)

    assert store.load_company_lineage(new_successor_cik) is None
    lineage = store.load_company_lineage(current_cik)
    assert lineage is not None
    assert lineage.canonical_current_cik == current_cik


def test_promoted_company_ingestion_receives_the_full_lineage(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    lineage_service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    lineage_service.apply_link(lineage_service.preview_link(current_cik, older_cik))
    store.apply_successor_promotion(
        lineage_service.preview_successor_promotion(current_cik, new_successor_cik)
    )
    discovered_lineages: list[tuple[str, ...]] = []

    class Gateway:
        def discover_company_window(
            self,
            lineage: object,
            *,
            annual_count: int,
            quarterly_count: int,
        ) -> DiscoveredCompanyWindow:
            assert annual_count == 1
            assert quarterly_count == 1
            members = lineage.current_to_oldest
            discovered_lineages.append(tuple(member.cik for member in members))
            return DiscoveredCompanyWindow(annual=(), quarterly=())

    class MappingFreeIngestionService(CompanyIngestionService):
        def _evaluate_mapping(self, ticker: str) -> tuple[None, None]:
            assert ticker == "TEST"
            return None, None

    ingestion_service = MappingFreeIngestionService(
        store=store,
        sec_gateway_factory=Gateway,  # type: ignore[arg-type]
        processor_factory=lambda: pytest.fail("empty discovery must not build a processor"),
    )

    result = ingestion_service.ingest_company_cik(
        new_successor_cik,
        annual_count=1,
        quarterly_count=1,
    )

    assert result.status == "checked_no_filings"
    assert discovered_lineages == [(new_successor_cik, current_cik, older_cik)]


def test_promotion_then_ingestion_preserves_immutable_filing_provenance(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    lineage_service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    lineage_service.apply_link(lineage_service.preview_link(current_cik, older_cik))
    with sqlite3.connect(database_path) as connection:
        company_id = connection.execute(
            "SELECT id FROM companies WHERE cik = ?", (current_cik,)
        ).fetchone()[0]
        cursor = connection.execute(
            "INSERT INTO filings(company_id, accession, form, filing_date, report_date, "
            "primary_document, source_url, is_active, active_window_rank) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 1)",
            (
                company_id,
                "0000000001-25-000001",
                "10-K",
                "2026-02-01",
                "2025-12-31",
                "older.htm",
                "https://www.sec.gov/Archives/older.htm",
            ),
        )
        filing_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO filing_provenance("
            "filing_id, company_id, registrant_cik, archive_owner_cik, recorded_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                filing_id,
                company_id,
                older_cik,
                older_cik,
                "2026-01-01T00:00:00+00:00",
            ),
        )
        immutable_before = connection.execute(
            "SELECT id, company_id, accession, form, filing_date, report_date, "
            "primary_document, source_url FROM filings WHERE id = ?",
            (filing_id,),
        ).fetchone()
        provenance_before = connection.execute(
            "SELECT * FROM filing_provenance WHERE filing_id = ?", (filing_id,)
        ).fetchone()

    store.apply_successor_promotion(
        lineage_service.preview_successor_promotion(current_cik, new_successor_cik)
    )

    successor_filing = Filing(
        accession="0000000003-26-000001",
        filing_date=date(2026, 8, 1),
        report_date=date(2026, 6, 30),
        form="10-K",
        primary_document="successor.htm",
        url="https://www.sec.gov/Archives/successor.htm",
        registrant_cik=new_successor_cik,
        archive_owner_cik=new_successor_cik,
    )

    class Gateway:
        def discover_company_window(
            self,
            _lineage: object,
            *,
            annual_count: int,
            quarterly_count: int,
        ) -> DiscoveredCompanyWindow:
            assert annual_count == 1
            assert quarterly_count == 1
            return DiscoveredCompanyWindow(annual=(successor_filing,), quarterly=())

    class MappingFreeIngestionService(CompanyIngestionService):
        def _evaluate_mapping(self, _ticker: str) -> tuple[None, None]:
            return None, None

    ingestion_service = MappingFreeIngestionService(
        store=store,
        sec_gateway_factory=Gateway,  # type: ignore[arg-type]
        processor_factory=_EmptyEvidenceProcessor,
    )
    first = ingestion_service.ingest_company_cik(
        new_successor_cik, annual_count=1, quarterly_count=1
    )
    second = ingestion_service.ingest_company_cik(
        new_successor_cik, annual_count=1, quarterly_count=1
    )

    with sqlite3.connect(database_path) as connection:
        immutable_after = connection.execute(
            "SELECT id, company_id, accession, form, filing_date, report_date, "
            "primary_document, source_url FROM filings WHERE id = ?",
            (filing_id,),
        ).fetchone()
        provenance_after = connection.execute(
            "SELECT * FROM filing_provenance WHERE filing_id = ?", (filing_id,)
        ).fetchone()
        counts = connection.execute(
            "SELECT (SELECT count(*) FROM filings), "
            "(SELECT count(*) FROM evidence_snapshots), "
            "(SELECT count(*) FROM filing_provenance)"
        ).fetchone()
        successor_provenance = connection.execute(
            "SELECT fp.registrant_cik, fp.archive_owner_cik FROM filing_provenance AS fp "
            "JOIN filings AS f ON f.id = fp.filing_id WHERE f.accession = ?",
            (successor_filing.accession,),
        ).fetchone()

    assert first.run is not None and first.run.filings[0].status == "stored"
    assert second.run is not None and second.run.filings[0].status == "reused"
    assert immutable_after == immutable_before
    assert provenance_after == provenance_before
    assert counts == (2, 1, 2)
    assert successor_provenance == (new_successor_cik, new_successor_cik)


def test_successor_promotion_rejects_equal_or_noncanonical_ciks(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    service.apply_link(service.preview_link(current_cik, older_cik))

    with pytest.raises(LineageError, match="must be different"):
        service.preview_successor_promotion(current_cik, current_cik)
    with pytest.raises(LineageError, match="must be the canonical current CIK"):
        service.preview_successor_promotion(older_cik, new_successor_cik)
    with pytest.raises(LineageError, match="already attached to this lineage"):
        service.preview_successor_promotion(current_cik, older_cik)


def test_successor_promotion_rejects_a_successor_owned_by_another_company(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    other_company_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    _insert_company(
        database_path,
        cik=other_company_cik,
        name="Other Corp",
        ticker="OTHER",
    )
    service = LineageMaintenanceService(
        store=EvidenceStore(database_path, database_path.parent),
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                other_company_cik: "Other Corp",
            }
        ),
    )

    with pytest.raises(LineageError, match="belongs to another stored company"):
        service.preview_successor_promotion(current_cik, other_company_cik)


def test_successor_promotion_rejects_a_conflicting_verified_date(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    store.apply_successor_promotion(
        service.preview_successor_promotion(
            current_cik,
            new_successor_cik,
            effective_date=date(2026, 5, 1),
        )
    )

    with pytest.raises(LineageError, match="already verified and cannot be changed"):
        service.preview_successor_promotion(
            current_cik,
            new_successor_cik,
            effective_date=date(2026, 6, 1),
        )


def test_successor_promotion_keeps_existing_adjacent_date_behavior(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(9)
    current_cik = "0000000002"
    older_cik = "0000000001"
    new_successor_cik = "0000000003"
    _insert_company(database_path, cik=current_cik, name="Current Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                current_cik: "Current Corp",
                older_cik: "Older Corp",
                new_successor_cik: "New Successor Corp",
            }
        ),
    )
    service.apply_link(
        service.preview_link(
            current_cik,
            older_cik,
            effective_date=date(2026, 6, 1),
        )
    )

    plan = service.preview_successor_promotion(
        current_cik,
        new_successor_cik,
        effective_date=date(2026, 5, 1),
    )

    assert tuple(edge.effective_date for edge in plan.after.edges) == (
        date(2026, 6, 1),
        date(2026, 5, 1),
    )
