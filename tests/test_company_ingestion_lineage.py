from datetime import date
from pathlib import Path

from sec_inline_financials.company_ingestion import CompanyIngestionService
from sec_inline_financials.company_lineage import (
    LineageMaintenanceService,
    RegistrantIdentity,
)
from sec_inline_financials.evidence_classification import REPORT_RULE_VERSION
from sec_inline_financials.evidence_models import (
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.sec_client import DiscoveredCompanyWindow
from sec_inline_financials.storage.evidence_store import EvidenceStore


class _Resolver:
    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        return RegistrantIdentity(cik=cik, legal_name=f"Registrant {cik}")


class _Gateway:
    def __init__(self, filing: Filing, quarterly: Filing | None = None) -> None:
        self.filing = filing
        self.quarterly = quarterly

    def resolve_company(self, ticker: str) -> Company:
        return Company(ticker=ticker, cik=self.filing.registrant_cik, name="Current Registrant")

    def discover_company_window(
        self, _lineage: object, *, annual_count: int, quarterly_count: int
    ) -> DiscoveredCompanyWindow:
        assert (annual_count, quarterly_count) == (1, 1)
        return DiscoveredCompanyWindow(
            annual=(self.filing,),
            quarterly=(self.quarterly,) if self.quarterly is not None else (),
        )


class _Processor:
    _profile = ExtractionProfile(
        application_version="test",
        extractor_version="test",
        arelle_version="test",
        validation_options=(),
        transform_plugin_revision="test",
        transform_plugin_hashes=(),
    )

    def extraction_profile(self) -> ExtractionProfile:
        return self._profile

    def extract_evidence(
        self, company: Company, filing: Filing, _capture_area: Path
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
            coverage_manifest=CoverageManifest(0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
            raw_log_json="[]",
        )


class _QuarterlyFailingProcessor(_Processor):
    def extract_evidence(
        self, company: Company, filing: Filing, capture_area: Path
    ) -> FilingEvidenceBundle:
        if filing.form == "10-Q":
            raise RuntimeError("quarterly fixture failed")
        return super().extract_evidence(company, filing, capture_area)


def test_cik_ingestion_processes_and_publishes_predecessor_filing(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Current Registrant")
    store.create_processing_run(company, purpose="seed", requested_window={})
    lineage_service = LineageMaintenanceService(store=store, registrant_resolver=_Resolver())
    lineage_service.apply_link(lineage_service.preview_link("2", "1"))
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
    gateway = _Gateway(predecessor_filing)
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: gateway,  # type: ignore[arg-type,return-value]
        processor_factory=_Processor,
        today_fn=lambda: date(2026, 3, 1),
    )

    result = service.ingest_company_cik("2", annual_count=1, quarterly_count=1)

    assert result.active_accessions == (predecessor_filing.accession,)
    mapping_inputs = store.list_mapping_inputs("NEW", "annual", REPORT_RULE_VERSION)
    assert mapping_inputs[0].registrant_ciks == ("0000000001",)


def test_partial_run_never_activates_a_filing_without_a_snapshot(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Current Registrant")
    store.create_processing_run(company, purpose="seed", requested_window={})
    annual = Filing(
        accession="0000000002-26-000001",
        filing_date=date(2026, 2, 1),
        report_date=date(2025, 12, 31),
        form="10-K",
        primary_document="annual.htm",
        url="https://www.sec.gov/Archives/example/annual.htm",
        registrant_cik="0000000002",
        archive_owner_cik="0000000002",
    )
    quarterly = Filing(
        accession="0000000002-25-000002",
        filing_date=date(2025, 11, 1),
        report_date=date(2025, 9, 30),
        form="10-Q",
        primary_document="quarter.htm",
        url="https://www.sec.gov/Archives/example/quarter.htm",
        registrant_cik="0000000002",
        archive_owner_cik="0000000002",
    )
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: _Gateway(annual, quarterly),  # type: ignore[arg-type]
        processor_factory=_QuarterlyFailingProcessor,
        today_fn=lambda: date(2026, 3, 1),
    )

    result = service.ingest_company_cik("2", annual_count=1, quarterly_count=1)

    assert result.active_accessions == (annual.accession,)
    state = store.get_company_state("NEW")
    assert state is not None
    assert state.active_filings_without_evidence == ()


def test_ticker_ingestion_uses_the_same_resolved_company_core(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    filing = Filing(
        accession="0000000002-26-000001",
        filing_date=date(2026, 2, 1),
        report_date=date(2025, 12, 31),
        form="10-K",
        primary_document="annual.htm",
        url="https://www.sec.gov/Archives/example/annual.htm",
        registrant_cik="0000000002",
        archive_owner_cik="0000000002",
    )
    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: _Gateway(filing),  # type: ignore[arg-type]
        processor_factory=_Processor,
        today_fn=lambda: date(2026, 3, 1),
    )

    result = service.ingest_company(
        "NEW",
        annual_count=1,
        quarterly_count=1,
        force_refresh=True,
    )

    assert result.active_accessions == (filing.accession,)
    assert store.load_company_lineage("2") is not None
