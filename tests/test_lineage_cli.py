from pathlib import Path

from sec_inline_financials.company_lineage import (
    LineageMaintenanceService,
    RegistrantIdentity,
)
from sec_inline_financials.errors import IngestionError
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


def test_operator_approves_displayed_names_with_y(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="Stored Current Name"),
        purpose="seed",
        requested_window={},
    )
    ingested: list[str] = []
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda cik: ingested.append(cik) or "ingested",
    )
    output: list[str] = []

    exit_code = run_lineage_command(
        service,
        "2",
        "1",
        input_fn=lambda _prompt: "y",
        output_fn=output.append,
    )

    rendered = "\n".join(output)
    assert exit_code == 0
    assert "0000000002  Current Legal Name" in rendered
    assert "0000000001  Predecessor Legal Name" in rendered
    assert "0000000001 -> 0000000002" in rendered
    assert ingested == ["0000000002"]
    assert store.load_company_lineage("2").current_to_oldest[-1].cik == "0000000001"


def test_operator_declines_without_mutation_or_ingestion(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="Stored Current Name"),
        purpose="seed",
        requested_window={},
    )
    ingested: list[str] = []
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda cik: ingested.append(cik),
    )
    output: list[str] = []

    exit_code = run_lineage_command(
        service,
        "2",
        "1",
        input_fn=lambda _prompt: "n",
        output_fn=output.append,
    )

    assert exit_code == 0
    assert output[-1] == "Cancelled; no changes made."
    assert ingested == []
    assert len(store.load_company_lineage("2").current_to_oldest) == 1


def test_ingestion_failure_retains_edge_for_retry(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    store.create_processing_run(
        Company(ticker="NEW", cik="0000000002", name="Stored Current Name"),
        purpose="seed",
        requested_window={},
    )

    def fail_ingestion(_cik: str) -> object:
        raise IngestionError("fixture SEC failure")

    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=fail_ingestion,
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
    assert "rerun this same command" in output[-1]
