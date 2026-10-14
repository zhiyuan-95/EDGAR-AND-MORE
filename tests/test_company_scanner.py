from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

import sec_inline_financials.company_ingestion as company_ingestion
from sec_inline_financials.company_ingestion import (
    CompanyIngestionService,
    IngestionSettings,
    scan_companies,
)
from sec_inline_financials.errors import DiscoveryError, IngestionError
from sec_inline_financials.evidence_ingestion import EvidenceIngestionService
from sec_inline_financials.evidence_models import (
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.sec_client import DiscoveredCompanyWindow
from sec_inline_financials.storage.evidence_store import EvidenceStore


class _EmptySecGateway:
    def __init__(
        self,
        companies: dict[str, Company],
        *,
        failing_tickers: frozenset[str] = frozenset(),
    ) -> None:
        self._companies = companies
        self._failing_tickers = failing_tickers
        self.resolved_tickers: list[str] = []

    def resolve_company(self, ticker: str) -> Company:
        self.resolved_tickers.append(ticker)
        if ticker in self._failing_tickers:
            raise DiscoveryError("SEC unavailable")
        return self._companies[ticker]

    def discover_company_window(
        self,
        _lineage: object,
        *,
        annual_count: int,
        quarterly_count: int,
    ) -> DiscoveredCompanyWindow:
        assert annual_count == 5
        assert quarterly_count == 12
        return DiscoveredCompanyWindow(annual=(), quarterly=())


class _SingleFilingSecGateway:
    def __init__(self, company: Company, filing: Filing) -> None:
        self._company = company
        self._filing = filing

    def resolve_company(self, ticker: str) -> Company:
        assert ticker == self._company.ticker
        return self._company

    def discover_company_window(
        self,
        _lineage: object,
        *,
        annual_count: int,
        quarterly_count: int,
    ) -> DiscoveredCompanyWindow:
        assert annual_count == 5
        assert quarterly_count == 12
        return DiscoveredCompanyWindow(annual=(self._filing,), quarterly=())


class _BundleProcessor:
    def __init__(self, bundle: FilingEvidenceBundle) -> None:
        self._bundle = bundle

    def extraction_profile(self) -> ExtractionProfile:
        return self._bundle.extraction_profile

    def extract_evidence(
        self,
        company: Company,
        filing: Filing,
        _capture_area: Path,
    ) -> FilingEvidenceBundle:
        assert company == self._bundle.company
        assert filing == self._bundle.filing
        return self._bundle


def test_scan_companies_checks_only_companies_whose_refresh_is_due(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    current = Company(ticker="CURRENT", cik="0000000001", name="Current Corp")
    due = Company(ticker="DUE", cik="0000000002", name="Due Corp")
    _seed_company_state(
        store,
        current,
        next_check_date_10k=date(2026, 11, 1),
        next_check_date_10q=date(2026, 11, 1),
    )
    _seed_company_state(
        store,
        due,
        next_check_date_10k=date(2026, 10, 12),
        next_check_date_10q=date(2026, 11, 1),
    )
    gateway = _EmptySecGateway({current.ticker: current, due.ticker: due})
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: object(),  # type: ignore[arg-type]
        today_fn=lambda: date(2026, 10, 12),
    )

    result = scan_companies(
        store=store,
        service=service,
        checked_on=date(2026, 10, 12),
    )

    assert [(item.company.ticker, item.status) for item in result.results] == [
        ("DUE", "checked_no_filings"),
    ]
    assert result.current_tickers == ("CURRENT",)
    assert result.failures == ()
    assert gateway.resolved_tickers == ["DUE"]


def test_scan_cli_reports_every_stored_company_and_summary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = IngestionSettings(
        database_path=tmp_path / "evidence.sqlite3",
        artifact_root=tmp_path / "artifacts",
    )
    store = EvidenceStore(settings.database_path, settings.artifact_root)
    store.initialize()
    _seed_company_state(
        store,
        Company(ticker="CURRENT", cik="0000000001", name="Current Corp"),
        next_check_date_10k=date(2026, 11, 1),
        next_check_date_10q=date(2026, 11, 1),
    )
    monkeypatch.setattr(
        company_ingestion.IngestionSettings,
        "from_environment",
        classmethod(lambda _cls: settings),
    )
    monkeypatch.setattr(company_ingestion, "date", _FixedDate)

    exit_code = company_ingestion.scan_main([])

    assert exit_code == 0
    output = capsys.readouterr().out
    assert "CURRENT: current" in output
    assert "Scan summary: 1 company; 0 due; 0 SEC checks; 0 updated; 0 failed." in output


def test_current_company_skips_direct_mapping(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    company = Company(ticker="CURRENT", cik="0000000001", name="Current Corp")
    _seed_company_state(
        store,
        company,
        next_check_date_10k=date(2026, 11, 1),
        next_check_date_10q=date(2026, 11, 1),
    )
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: (_ for _ in ()).throw(
            AssertionError("a current company must not contact the SEC")
        ),
        processor_factory=lambda: object(),  # type: ignore[arg-type]
        today_fn=lambda: date(2026, 10, 12),
    )

    result = service.ingest_company(company.ticker)

    assert result.status == "reused_local"
    assert result.checked_sec is False
    assert result.mapping is None
    assert result.mapping_error is None


def test_due_company_with_no_new_filing_skips_direct_mapping(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    company = Company(ticker="DUE", cik="0000000001", name="Due Corp")
    _seed_company_state(
        store,
        company,
        next_check_date_10k=date(2026, 10, 12),
        next_check_date_10q=date(2026, 10, 12),
    )
    gateway = _EmptySecGateway({company.ticker: company})
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: object(),  # type: ignore[arg-type]
        today_fn=lambda: date(2026, 10, 12),
    )

    result = service.ingest_company(company.ticker)

    assert result.status == "checked_no_filings"
    assert result.checked_sec is True
    assert result.mapping is None
    assert result.mapping_error is None


def test_due_company_with_unchanged_filing_skips_direct_mapping(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    company = Company(ticker="DUE", cik="0000000001", name="Due Corp")
    _seed_company_state(
        store,
        company,
        next_check_date_10k=date(2026, 10, 12),
        next_check_date_10q=date(2026, 10, 12),
    )
    filing = _annual_filing(company)
    gateway = _SingleFilingSecGateway(company, filing)
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: _BundleProcessor(_evidence_bundle(company, filing)),
        today_fn=lambda: date(2026, 10, 12),
    )

    result = service.ingest_company(company.ticker)

    assert result.status == "checked_no_update"
    assert result.new_accessions == ()
    assert result.mapping is None
    assert result.mapping_error is None


def test_new_filing_runs_direct_mapping_after_publication(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    company = Company(ticker="NEW", cik="0000000001", name="New Corp")
    filing = _annual_filing(company)
    gateway = _SingleFilingSecGateway(company, filing)
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: _BundleProcessor(_evidence_bundle(company, filing)),
        today_fn=lambda: date(2026, 10, 12),
    )

    result = service.ingest_company(company.ticker)

    assert result.status == "initialized"
    assert result.new_accessions == (filing.accession,)
    assert result.mapping is not None
    assert result.mapping.annual is not None
    assert result.mapping_error is None


def test_scan_companies_continues_after_one_company_fails(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    failed = Company(ticker="FAIL", cik="0000000001", name="Fail Corp")
    successful = Company(ticker="GOOD", cik="0000000002", name="Good Corp")
    for company in (failed, successful):
        store.publish_filing_window(
            company,
            annual=[],
            quarterly=[],
            snapshot_ids={},
            next_check_date_10k=date(2026, 10, 12),
            next_check_date_10q=date(2026, 10, 12),
        )
    gateway = _EmptySecGateway(
        {failed.ticker: failed, successful.ticker: successful},
        failing_tickers=frozenset({failed.ticker}),
    )
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: object(),  # type: ignore[arg-type]
        today_fn=lambda: date(2026, 10, 12),
    )

    result = scan_companies(
        store=store,
        service=service,
        checked_on=date(2026, 10, 12),
    )

    assert [(item.company.ticker, item.status) for item in result.results] == [
        ("GOOD", "checked_no_filings")
    ]
    assert [(item.ticker, item.error) for item in result.failures] == [("FAIL", "SEC unavailable")]
    assert gateway.resolved_tickers == ["FAIL", "GOOD"]


def test_scan_companies_counts_failed_refresh_with_local_fallback(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    company = Company(ticker="STALE", cik="0000000001", name="Stale Corp")
    _seed_company_state(
        store,
        company,
        next_check_date_10k=date(2026, 10, 12),
        next_check_date_10q=date(2026, 10, 12),
    )
    gateway = _EmptySecGateway(
        {company.ticker: company},
        failing_tickers=frozenset({company.ticker}),
    )
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,
        processor_factory=lambda: object(),  # type: ignore[arg-type]
        today_fn=lambda: date(2026, 10, 12),
    )

    result = scan_companies(
        store=store,
        service=service,
        checked_on=date(2026, 10, 12),
    )

    assert [item.status for item in result.results] == ["refresh_failed_using_local_data"]
    assert result.failed_count == 1
    assert result.has_failures is True


def test_scan_companies_rejects_invalid_window_before_an_empty_scan(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: object(),  # type: ignore[arg-type,return-value]
        processor_factory=lambda: object(),  # type: ignore[arg-type]
    )

    with pytest.raises(
        IngestionError,
        match="Annual and quarterly filing counts must both be positive",
    ):
        scan_companies(store=store, service=service, annual_count=0)


def _seed_company_state(
    store: EvidenceStore,
    company: Company,
    *,
    next_check_date_10k: date,
    next_check_date_10q: date,
) -> None:
    filing = _annual_filing(company)
    bundle = _evidence_bundle(company, filing)
    outcome = EvidenceIngestionService(
        sec_client=object(),  # type: ignore[arg-type]
        processor=_BundleProcessor(bundle),
        store=store,
    ).ingest_filing(company, filing)
    assert outcome.snapshot_id is not None
    store.publish_filing_window(
        company,
        annual=[],
        quarterly=[],
        snapshot_ids={},
        next_check_date_10k=next_check_date_10k,
        next_check_date_10q=next_check_date_10q,
    )


def _annual_filing(company: Company) -> Filing:
    return Filing(
        accession=f"{company.cik}-25-000001",
        filing_date=date(2025, 2, 1),
        report_date=date(2024, 12, 31),
        form="10-K",
        primary_document=f"{company.ticker.lower()}-20241231.htm",
        url=f"https://www.sec.gov/Archives/{company.ticker.lower()}-20241231.htm",
        registrant_cik=company.cik,
        archive_owner_cik=company.cik,
    )


def _evidence_bundle(company: Company, filing: Filing) -> FilingEvidenceBundle:
    return FilingEvidenceBundle(
        company=company,
        filing=filing,
        captured_company_name=company.name,
        captured_company_ticker=company.ticker,
        fiscal_year=2024,
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
        extraction_profile=ExtractionProfile(
            application_version="test",
            extractor_version="test",
            arelle_version="test",
            validation_options=(),
            transform_plugin_revision="test",
            transform_plugin_hashes=(),
            serialization_version="evidence-v3",
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
            source_document_count=0,
        ),
        raw_log_json="{}",
    )


class _FixedDate(date):
    @classmethod
    def today(cls) -> date:
        return cls(2026, 10, 12)
