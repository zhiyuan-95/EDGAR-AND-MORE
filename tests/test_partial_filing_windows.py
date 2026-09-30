from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest

import sec_inline_financials.company_ingestion as company_ingestion
from sec_inline_financials.company_ingestion import CompanyIngestionService
from sec_inline_financials.errors import MappingInputError
from sec_inline_financials.evidence_ingestion import EvidenceIngestionService
from sec_inline_financials.evidence_models import FilingOutcome, RunOutcome, StoredCompanyState
from sec_inline_financials.mapping_models import MappingSnapshotInput, MetricEvaluationRef
from sec_inline_financials.mapping_service import DirectMappingService
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.sec_client import SecClient
from sec_inline_financials.storage.evidence_store import EvidenceStore

COMPANY = Company(ticker="NEW", cik="0000000123", name="New Company")
QUARTERLY = Filing(
    accession="0000000123-26-000001",
    filing_date=date(2026, 5, 1),
    report_date=date(2026, 3, 31),
    form="10-Q",
    primary_document="new-20260331.htm",
    url=("https://www.sec.gov/Archives/edgar/data/123/000000012326000001/new-20260331.htm"),
)


def test_sec_client_returns_available_filings_when_history_is_short() -> None:
    payload = {
        "filings": {
            "recent": {
                "accessionNumber": [QUARTERLY.accession],
                "form": [QUARTERLY.form],
                "isInlineXBRL": [1],
                "filingDate": [QUARTERLY.filing_date.isoformat()],
                "reportDate": [QUARTERLY.report_date.isoformat()],
                "primaryDocument": [QUARTERLY.primary_document],
            },
            "files": [],
        }
    }
    client = SecClient(user_agent="Test Operator test@example.com", fetch_json=lambda *_: payload)

    assert client.discover_annual_inline_filings(COMPANY, count=5) == []
    assert client.discover_quarterly_inline_filings(COMPANY, count=12) == [QUARTERLY]


@pytest.mark.parametrize("annual, quarterly", [([], [QUARTERLY]), ([], [])])
def test_store_publishes_incomplete_or_empty_filing_windows(
    tmp_path: Path,
    annual: list[Filing],
    quarterly: list[Filing],
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()

    store.publish_filing_window(
        COMPANY,
        annual=annual,
        quarterly=quarterly,
        snapshot_ids={},
        next_check_date_10k=date(2026, 10, 1),
        next_check_date_10q=date(2026, 10, 1),
    )

    state = store.get_company_state(COMPANY.ticker)
    assert state is not None
    assert state.latest_10k_filing_date is None
    assert state.latest_10q_filing_date == (QUARTERLY.filing_date if quarterly else None)
    assert state.active_accessions == tuple(filing.accession for filing in quarterly)


def test_empty_selected_window_is_a_successful_no_work_run(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    service = EvidenceIngestionService(
        sec_client=_Gateway([]),
        processor=object(),  # type: ignore[arg-type]
        store=store,
    )

    result = service.ingest_selected_window(
        COMPANY,
        annual=[],
        quarterly=[],
        annual_count=5,
        quarterly_count=12,
    )

    assert result.status == "succeeded"
    assert result.filings == ()


class _Gateway:
    def __init__(self, quarterly: list[Filing]) -> None:
        self._quarterly = quarterly

    def resolve_company(self, ticker: str) -> Company:
        assert ticker == COMPANY.ticker
        return COMPANY

    def discover_annual_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        assert company == COMPANY
        assert count == 5
        return []

    def discover_quarterly_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        assert company == COMPANY
        assert count == 12
        return self._quarterly


class _Store:
    def __init__(self) -> None:
        self.state: StoredCompanyState | None = None
        self.published_snapshot_ids: dict[str, int] | None = None

    def initialize(self) -> None:
        return None

    def get_company_state(self, ticker: str) -> StoredCompanyState | None:
        assert ticker == COMPANY.ticker
        return self.state

    def get_company_state_by_cik(self, cik: str) -> StoredCompanyState | None:
        assert cik == COMPANY.cik
        return None

    def fiscal_period_for_accession(self, accession: str) -> str | None:
        assert accession == QUARTERLY.accession
        return "Q1"

    def publish_filing_window(
        self,
        company: Company,
        *,
        annual: list[Filing],
        quarterly: list[Filing],
        snapshot_ids: dict[str, int],
        next_check_date_10k: date,
        next_check_date_10q: date,
    ) -> None:
        self.published_snapshot_ids = snapshot_ids
        self.state = StoredCompanyState(
            company=company,
            latest_10k_filing_date=None,
            latest_10q_filing_date=quarterly[0].filing_date if quarterly else None,
            next_check_date_10k=next_check_date_10k,
            next_check_date_10q=next_check_date_10q,
            snapshot_count=len(snapshot_ids),
            known_accessions=tuple(filing.accession for filing in quarterly),
            active_accessions=tuple(filing.accession for filing in quarterly),
        )


class _MappingFreeCompanyIngestionService(CompanyIngestionService):
    def _evaluate_mapping(self, ticker: str) -> tuple[None, None]:
        assert ticker == COMPANY.ticker
        return None, None


def test_company_ingestion_processes_a_short_window(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    class FakeEvidenceIngestionService:
        def __init__(self, **_: object) -> None:
            pass

        def ingest_selected_window(
            self,
            company: Company,
            *,
            annual: list[Filing],
            quarterly: list[Filing],
            annual_count: int | None = None,
            quarterly_count: int | None = None,
        ) -> RunOutcome:
            captured.update(
                company=company,
                annual=annual,
                quarterly=quarterly,
                annual_count=annual_count,
                quarterly_count=quarterly_count,
            )
            return RunOutcome(
                run_id=1,
                status="succeeded",
                filings=(
                    FilingOutcome(
                        accession=QUARTERLY.accession,
                        status="stored",
                        snapshot_id=7,
                    ),
                ),
            )

    monkeypatch.setattr(
        company_ingestion,
        "EvidenceIngestionService",
        FakeEvidenceIngestionService,
    )
    store = _Store()
    service = _MappingFreeCompanyIngestionService(
        store=store,  # type: ignore[arg-type]
        sec_gateway_factory=lambda: _Gateway([QUARTERLY]),
        processor_factory=object,
        today_fn=lambda: date(2026, 9, 30),
    )

    result = service.ingest_company(COMPANY.ticker)

    assert result.status == "initialized"
    assert result.error is None
    assert result.coverage_warning == (
        "SEC returned fewer eligible filings than requested "
        "(annual 0/5, quarterly 1/12); processing all 1 filing(s) found."
    )
    assert captured["annual_count"] == 5
    assert captured["quarterly_count"] == 12
    assert store.published_snapshot_ids == {QUARTERLY.accession: 7}


def test_company_ingestion_treats_no_eligible_filings_as_a_non_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class UnexpectedEvidenceIngestionService:
        def __init__(self, **_: object) -> None:
            raise AssertionError("No processor should be built when discovery returns no filings.")

    monkeypatch.setattr(
        company_ingestion,
        "EvidenceIngestionService",
        UnexpectedEvidenceIngestionService,
    )
    store = _Store()
    service = _MappingFreeCompanyIngestionService(
        store=store,  # type: ignore[arg-type]
        sec_gateway_factory=lambda: _Gateway([]),
        processor_factory=object,
        today_fn=lambda: date(2026, 9, 30),
    )

    result = service.ingest_company(COMPANY.ticker)

    assert result.status == "checked_no_filings"
    assert result.run is None
    assert result.error is None
    assert store.published_snapshot_ids == {}


class _MappingStore:
    def initialize(self) -> None:
        return None

    def get_company_state(self, ticker: str) -> StoredCompanyState:
        return StoredCompanyState(
            company=COMPANY,
            latest_10k_filing_date=None,
            latest_10q_filing_date=QUARTERLY.filing_date,
            next_check_date_10k=None,
            next_check_date_10q=None,
            snapshot_count=1,
        )

    def list_mapping_inputs(
        self, ticker: str, report_kind: str, report_rule_version: str
    ) -> tuple[MappingSnapshotInput, ...]:
        if report_kind == "annual":
            raise MappingInputError("No active annual filing window is stored for NEW.")
        return (
            MappingSnapshotInput(
                snapshot_id=7,
                filing_id=8,
                accession=QUARTERLY.accession,
                form=QUARTERLY.form,
                report_date=QUARTERLY.report_date,
                active_window_rank=1,
                payload_hash="payload",
                report_evaluation_id=9,
                source_report_rule_version="report-v1",
                company_cik=COMPANY.cik,
            ),
        )


class _DirectMappingService(DirectMappingService):
    def _evaluate_kind(
        self,
        ticker: str,
        report_kind: str,
        snapshots: tuple[MappingSnapshotInput, ...],
    ) -> MetricEvaluationRef:
        assert ticker == COMPANY.ticker
        assert report_kind == "quarterly"
        assert len(snapshots) == 1
        return MetricEvaluationRef(
            evaluation_id=10,
            company_id=11,
            report_kind="quarterly",
            definition_version="direct-mapping-v1",
            mapping_rule_version="rules-v1",
            mapping_rule_hash="rules-hash",
            source_report_rule_version="report-v1",
            active_window_hash="window-hash",
            reported_count=1,
            missing_count=0,
            reused=False,
            stale=False,
        )


def test_available_mapping_runs_each_non_empty_form_independently() -> None:
    service = _DirectMappingService(_MappingStore())  # type: ignore[arg-type]

    result = service.evaluate_company(COMPANY.ticker, require_complete_window=False)

    assert result.annual is None
    assert result.annual_error == "No active annual filing window is stored for NEW."
    assert result.quarterly is not None
    assert result.quarterly.reported_count == 1
    assert result.quarterly_error is None
