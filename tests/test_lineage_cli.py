from pathlib import Path

from sec_inline_financials.company_ingestion import CompanyIngestionResult
from sec_inline_financials.company_lineage import LineageMaintenanceService, RegistrantIdentity
from sec_inline_financials.lineage_cli import run_lineage_command
from sec_inline_financials.models import Company
from sec_inline_financials.storage.evidence_store import EvidenceStore


class _Resolver:
    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        names = {
            "0000000002": "Current Legal Name",
            "0000000001": "Predecessor Legal Name",
        }
        return RegistrantIdentity(cik=cik, legal_name=names[cik])


def test_empty_lineage_ingestion_retains_edge_and_exits_nonzero(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: CompanyIngestionResult(
            company=company,
            status="checked_no_filings",
            checked_sec=True,
            annual_check_due=True,
            quarterly_check_due=True,
        ),
    )
    output: list[str] = []

    exit_code = run_lineage_command(
        service,
        "2",
        "1",
        input_fn=lambda _prompt: "y",
        output_fn=output.append,
    )

    assert exit_code == 1
    assert store.load_company_lineage("2").current_to_oldest[-1].cik == "0000000001"
    assert "Ingestion: checked_no_filings" in output
    assert "Lineage relationship was retained." in output
    assert "Rerun: sec-inline-financials-lineage 2 1" in output
