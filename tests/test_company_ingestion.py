from __future__ import annotations

import hashlib
from datetime import date
from pathlib import Path

import pytest

from sec_inline_financials.company_ingestion import (
    CompanyIngestionService,
    _add_months,
    _next_market_day,
    _previous_market_day,
)
from sec_inline_financials.errors import DiscoveryError, IngestionError
from sec_inline_financials.evidence_models import (
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
    SourceDocumentRecord,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage.evidence_store import EvidenceStore


def _filing(
    accession: str,
    *,
    form: str,
    filing_date: date,
    report_date: date,
) -> Filing:
    document = f"{accession}.htm"
    return Filing(
        accession=accession,
        filing_date=filing_date,
        report_date=report_date,
        form=form,
        primary_document=document,
        url=f"https://www.sec.gov/Archives/edgar/data/123/{accession}/{document}",
    )


def _empty_bundle(
    tmp_path: Path,
    company: Company,
    filing: Filing,
    *,
    fiscal_period: str,
) -> FilingEvidenceBundle:
    source_bytes = f"<html>{filing.accession}</html>".encode()
    source_path = tmp_path / f"{filing.accession}.htm"
    source_path.write_bytes(source_bytes)
    return FilingEvidenceBundle(
        company=company,
        filing=filing,
        captured_company_name=company.name,
        captured_company_ticker=company.ticker,
        fiscal_year=filing.report_date.year,
        fiscal_period=fiscal_period,
        fiscal_year_source="fixture",
        fiscal_period_source="fixture",
        source_documents=(
            SourceDocumentRecord(
                key="primary",
                original_uri=filing.url,
                document_kind="inline XBRL instance",
                retention_kind="retained_original",
                captured_path=str(source_path),
                content_hash=hashlib.sha256(source_bytes).hexdigest(),
                byte_size=len(source_bytes),
                media_type="text/html",
            ),
        ),
        concepts=(),
        concept_labels=(),
        contexts=(),
        units=(),
        observations=(),
        diagnostics=(),
        validation_messages=(),
        calculation_networks=(),
        calculation_relationships=(),
        extraction_profile=ExtractionProfile(
            application_version="0.1.0",
            extractor_version="fixture",
            arelle_version="fixture",
            validation_options=(),
            transform_plugin_revision="fixture",
            transform_plugin_hashes=(),
        ),
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
            source_document_count=1,
        ),
        raw_log_json='{"log":[]}',
    )


class _Gateway:
    def __init__(self, company: Company, annual: list[Filing], quarterly: list[Filing]) -> None:
        self.company = company
        self.annual = annual
        self.quarterly = quarterly
        self.calls: list[str] = []

    def resolve_company(self, ticker: str) -> Company:
        self.calls.append(f"resolve:{ticker}")
        return self.company

    def discover_annual_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        assert company == self.company
        self.calls.append("annual")
        return self.annual[:count]

    def discover_quarterly_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        assert company == self.company
        self.calls.append("quarterly")
        return self.quarterly[:count]


class _Processor:
    def __init__(self, bundles: dict[str, FilingEvidenceBundle]) -> None:
        self.bundles = bundles
        self.extracted: list[str] = []

    def extraction_profile(self) -> ExtractionProfile:
        return next(iter(self.bundles.values())).extraction_profile

    def extract_evidence(
        self, company: Company, filing: Filing, _capture_area: Path
    ) -> FilingEvidenceBundle:
        bundle = self.bundles[filing.accession]
        assert bundle.company == company
        self.extracted.append(filing.accession)
        return bundle


def _fixture_services(
    tmp_path: Path,
) -> tuple[
    EvidenceStore,
    Company,
    Filing,
    Filing,
    _Gateway,
    _Processor,
]:
    company = Company(ticker="TEST", cik="0000000123", name="Test Company")
    annual = _filing(
        "0000000123-25-000001",
        form="10-K",
        filing_date=date(2025, 2, 3),
        report_date=date(2024, 12, 31),
    )
    quarterly = _filing(
        "0000000123-25-000002",
        form="10-Q",
        filing_date=date(2025, 11, 3),
        report_date=date(2025, 9, 30),
    )
    bundles = {
        annual.accession: _empty_bundle(tmp_path, company, annual, fiscal_period="FY"),
        quarterly.accession: _empty_bundle(tmp_path, company, quarterly, fiscal_period="Q3"),
    }
    gateway = _Gateway(company, [annual], [quarterly])
    processor = _Processor(bundles)
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    return store, company, annual, quarterly, gateway, processor


def test_initial_ingestion_publishes_refresh_state_and_local_access_skips_sec(
    tmp_path: Path,
) -> None:
    store, company, annual, quarterly, gateway, processor = _fixture_services(tmp_path)
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 1, 5),
    )

    initialized = service.ingest_company("test", annual_count=1, quarterly_count=1)

    assert initialized.status == "initialized"
    assert initialized.checked_sec is True
    assert initialized.new_accessions == (annual.accession, quarterly.accession)
    assert set(processor.extracted) == {annual.accession, quarterly.accession}
    state = store.get_company_state("TEST")
    assert state is not None
    assert state.latest_10k_filing_date == date(2025, 2, 3)
    assert state.latest_10q_filing_date == date(2025, 11, 3)
    assert state.next_check_date_10k == date(2026, 2, 3)
    assert state.next_check_date_10q == date(2026, 5, 1)
    assert state.active_accessions == (annual.accession, quarterly.accession)
    assert state.active_filings_without_evidence == ()

    local = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: (_ for _ in ()).throw(AssertionError("SEC was called")),
        processor_factory=lambda: (_ for _ in ()).throw(AssertionError("Arelle was called")),
        today_fn=lambda: date(2026, 1, 6),
    ).ingest_company("TEST", annual_count=1, quarterly_count=1)

    assert local.status == "reused_local"
    assert local.checked_sec is False
    assert local.run is None


def test_due_check_without_new_accession_reuses_snapshots_and_advances_due_form(
    tmp_path: Path,
) -> None:
    store, _company, _annual, _quarterly, gateway, processor = _fixture_services(tmp_path)
    CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 1, 5),
    ).ingest_company("TEST", annual_count=1, quarterly_count=1)
    extracted_before = tuple(processor.extracted)

    result = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 2, 3),
    ).ingest_company("TEST", annual_count=1, quarterly_count=1)

    assert result.status == "checked_no_update"
    assert result.annual_check_due is True
    assert result.quarterly_check_due is False
    assert result.new_accessions == ()
    assert result.run is not None
    assert {item.status for item in result.run.filings} == {"reused"}
    assert tuple(processor.extracted) == extracted_before
    state = store.get_company_state("TEST")
    assert state is not None
    assert state.next_check_date_10k == date(2026, 2, 4)
    assert state.next_check_date_10q == date(2026, 5, 1)


def test_new_filing_extends_history_and_moves_the_active_window(tmp_path: Path) -> None:
    store, company, annual, old_quarterly, gateway, processor = _fixture_services(tmp_path)
    CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 1, 5),
    ).ingest_company("TEST", annual_count=1, quarterly_count=1)
    new_quarterly = _filing(
        "0000000123-26-000003",
        form="10-Q",
        filing_date=date(2026, 2, 2),
        report_date=date(2025, 12, 31),
    )
    processor.bundles[new_quarterly.accession] = _empty_bundle(
        tmp_path, company, new_quarterly, fiscal_period="Q1"
    )
    gateway.quarterly = [new_quarterly]

    result = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 2, 4),
    ).ingest_company("TEST", annual_count=1, quarterly_count=1)

    assert result.status == "updated"
    assert result.new_accessions == (new_quarterly.accession,)
    assert result.active_accessions == (annual.accession, new_quarterly.accession)
    state = store.get_company_state("TEST")
    assert state is not None
    assert state.snapshot_count == 3
    assert old_quarterly.accession in state.known_accessions
    with store.database.connection() as connection:
        old_active = connection.execute(
            "SELECT is_active FROM filings WHERE accession = ?", (old_quarterly.accession,)
        ).fetchone()[0]
    assert old_active == 0


def test_failed_due_refresh_preserves_the_published_local_window(tmp_path: Path) -> None:
    store, _company, _annual, _quarterly, gateway, processor = _fixture_services(tmp_path)
    CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 1, 5),
    ).ingest_company("TEST", annual_count=1, quarterly_count=1)
    before = store.get_company_state("TEST")
    assert before is not None

    def fail_discovery() -> _Gateway:
        raise DiscoveryError("SEC unavailable")

    result = CompanyIngestionService(
        store=store,
        sec_gateway_factory=fail_discovery,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 2, 3),
    ).ingest_company("TEST", annual_count=1, quarterly_count=1)

    assert result.status == "refresh_failed_using_local_data"
    assert result.checked_sec is True
    assert result.active_accessions == before.active_accessions
    assert "SEC unavailable" in (result.error or "")
    assert store.get_company_state("TEST") == before


def test_initial_discovery_failure_propagates_and_market_calendar_is_deterministic(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")

    def fail_discovery() -> _Gateway:
        raise DiscoveryError("SEC unavailable")

    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=fail_discovery,
        processor_factory=lambda: (_ for _ in ()).throw(AssertionError("not reached")),
        today_fn=lambda: date(2026, 1, 5),
    )
    with pytest.raises(DiscoveryError, match="SEC unavailable"):
        service.ingest_company("TEST", annual_count=1, quarterly_count=1)

    assert _add_months(date(2024, 2, 29), 12) == date(2025, 2, 28)
    assert _previous_market_day(date(2026, 7, 4)) == date(2026, 7, 2)
    assert _next_market_day(date(2026, 12, 31)) == date(2027, 1, 4)


def test_initial_all_filing_failures_raise_ingestion_error(tmp_path: Path) -> None:
    store, _company, _annual, _quarterly, gateway, processor = _fixture_services(tmp_path)

    def fail_extract(
        _company: Company, _filing: Filing, _capture_area: Path
    ) -> FilingEvidenceBundle:
        raise RuntimeError("Arelle failed")

    processor.extract_evidence = fail_extract  # type: ignore[method-assign]
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: processor,
        today_fn=lambda: date(2026, 1, 5),
    )

    with pytest.raises(IngestionError, match="no usable filing evidence"):
        service.ingest_company("TEST", annual_count=1, quarterly_count=1)
