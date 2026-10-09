from datetime import date
from pathlib import Path

import pytest

from sec_inline_financials.company_ingestion import CompanyIngestionResult
from sec_inline_financials.company_lineage import LineageMaintenanceService, RegistrantIdentity
from sec_inline_financials.errors import DiscoveryError
from sec_inline_financials.lineage_cli import (
    run_lineage_command,
    run_successor_promotion_command,
)
from sec_inline_financials.models import Company
from sec_inline_financials.storage.evidence_store import EvidenceStore


class _Resolver:
    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        names = {
            "0000000002": "Current Legal Name",
            "0000000001": "Predecessor Legal Name",
            "0000000003": "New Successor Legal Name",
        }
        return RegistrantIdentity(cik=cik, legal_name=names[cik])


class _WrongCikResolver:
    def resolve_registrant(self, _cik: str) -> RegistrantIdentity:
        return RegistrantIdentity(cik="0000000999", legal_name="Wrong Registrant")


class _BlankNameResolver:
    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        return RegistrantIdentity(cik=cik, legal_name="  ")


class _UnavailableResolver:
    def resolve_registrant(self, _cik: str) -> RegistrantIdentity:
        raise DiscoveryError("SEC unavailable")


def test_successor_promotion_command_uses_the_approved_prompt_and_ingests_successor(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    stored_company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    promoted_company = Company(
        ticker="NEW",
        cik="0000000003",
        name="New Successor Legal Name",
    )
    store.create_processing_run(stored_company, purpose="seed", requested_window={})
    ingested: list[str] = []

    def ingest(cik: str) -> CompanyIngestionResult:
        ingested.append(cik)
        return CompanyIngestionResult(
            company=promoted_company,
            status="updated",
            checked_sec=True,
            annual_check_due=True,
            quarterly_check_due=True,
        )

    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=ingest,
    )
    prompts: list[str] = []
    output: list[str] = []

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        input_fn=lambda prompt: prompts.append(prompt) or "y",
        output_fn=output.append,
    )

    assert exit_code == 0
    assert prompts == ["Create this lineage and ingest the successor's filings? [y/n]: "]
    assert ingested == ["0000000003"]
    assert "  0000000002 -> 0000000003" in output
    assert "  Status: new edge" in output
    assert "Lineage: created" in output
    assert "Ingestion: updated" in output
    lineage = store.load_company_lineage("3")
    assert lineage is not None
    assert lineage.canonical_current_cik == "0000000003"


def test_successor_promotion_ingestion_failure_retains_lineage_and_prints_exact_rerun(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})

    def fail_ingestion(_cik: str) -> CompanyIngestionResult:
        raise DiscoveryError("SEC unavailable")

    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=fail_ingestion,
    )
    output: list[str] = []

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        effective_date=None,
        input_fn=lambda _prompt: "y",
        output_fn=output.append,
    )

    lineage = store.load_company_lineage("3")
    assert exit_code == 1
    assert lineage is not None
    assert lineage.canonical_current_cik == "0000000003"
    assert "Error: SEC unavailable" in output
    assert (
        "If the lineage was committed before ingestion failed, rerun this same command." in output
    )
    assert "Rerun: sec-inline-financials-lineage-change 2 3" in output


@pytest.mark.parametrize(
    ("resolver", "error_text"),
    (
        (_WrongCikResolver(), "did not match requested CIK"),
        (_BlankNameResolver(), "has no legal name"),
        (_UnavailableResolver(), "SEC unavailable"),
    ),
)
@pytest.mark.parametrize("direction", ["predecessor", "successor"])
def test_lineage_commands_reject_identity_failures_before_confirmation(
    tmp_path: Path,
    resolver: object,
    error_text: str,
    direction: str,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=resolver,  # type: ignore[arg-type]
        ingest_company_cik=lambda _cik: pytest.fail("identity failure must not ingest"),
    )
    output: list[str] = []

    if direction == "predecessor":
        exit_code = run_lineage_command(
            service,
            "2",
            "1",
            input_fn=lambda _prompt: pytest.fail("identity failure must not prompt"),
            output_fn=output.append,
        )
    else:
        exit_code = run_successor_promotion_command(
            service,
            "2",
            "3",
            input_fn=lambda _prompt: pytest.fail("identity failure must not prompt"),
            output_fn=output.append,
        )

    lineage = store.load_company_lineage("2")
    assert exit_code == 1
    assert any(line == f"Error: {error_text}" or error_text in line for line in output)
    assert lineage is not None
    assert lineage.current_to_oldest == (RegistrantIdentity("0000000002", "Stored Current Name"),)


@pytest.mark.parametrize("answer", ["n", ""])
def test_cancelled_successor_promotion_does_not_mutate_or_ingest(
    tmp_path: Path,
    answer: str,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: pytest.fail("cancelled promotion must not ingest"),
    )
    output: list[str] = []

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        input_fn=lambda _prompt: answer,
        output_fn=output.append,
    )

    assert exit_code == 0
    assert store.load_company_lineage("3") is None
    assert "Lineage cancelled; no lineage or ingestion changes made." in output


def test_successor_promotion_repeats_invalid_confirmation_and_accepts_uppercase_y(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: CompanyIngestionResult(
            company=Company(
                ticker="NEW",
                cik="0000000003",
                name="New Successor Legal Name",
            ),
            status="updated",
            checked_sec=True,
            annual_check_due=True,
            quarterly_check_due=True,
        ),
    )
    answers = iter(["maybe", "Y"])
    output: list[str] = []

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        input_fn=lambda _prompt: next(answers),
        output_fn=output.append,
    )

    assert exit_code == 0
    assert output.count("Please enter y or n.") == 1
    lineage = store.load_company_lineage("3")
    assert lineage is not None
    assert lineage.canonical_current_cik == "0000000003"


def test_successor_promotion_eof_cancels_without_mutation(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: pytest.fail("EOF cancellation must not ingest"),
    )

    def end_input(_prompt: str) -> str:
        raise EOFError

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        input_fn=end_input,
        output_fn=lambda _message: None,
    )

    assert exit_code == 0
    assert store.load_company_lineage("3") is None


def test_successor_promotion_ctrl_c_before_approval_cancels_without_mutation(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: pytest.fail("pre-approval interrupt must not ingest"),
    )
    output: list[str] = []

    def interrupt(_prompt: str) -> str:
        raise KeyboardInterrupt

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        input_fn=interrupt,
        output_fn=output.append,
    )

    assert exit_code == 130
    assert store.load_company_lineage("3") is None
    assert "Lineage cancelled; no lineage or ingestion changes made." in output


def test_successor_promotion_rerun_renders_new_canonical_and_already_present(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: CompanyIngestionResult(
            company=Company(ticker="NEW", cik="0000000003", name="New Successor Legal Name"),
            status="updated",
            checked_sec=True,
            annual_check_due=True,
            quarterly_check_due=True,
        ),
    )
    assert run_successor_promotion_command(service, "2", "3", input_fn=lambda _: "y") == 0
    output: list[str] = []

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        input_fn=lambda _prompt: "y",
        output_fn=output.append,
    )

    assert exit_code == 0
    assert "  New Successor Legal Name (current CIK 0000000003)" in output
    assert "  Status: already present" in output
    assert "Lineage: already_present" in output


def test_successor_promotion_interruption_after_approval_prints_exact_rerun(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})

    def interrupt_ingestion(_cik: str) -> CompanyIngestionResult:
        raise KeyboardInterrupt

    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=interrupt_ingestion,
    )
    output: list[str] = []

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        input_fn=lambda _prompt: "y",
        output_fn=output.append,
    )

    assert exit_code == 130
    assert store.load_company_lineage("3") is not None
    assert "Interrupted after approval; the lineage may already be committed." in output
    assert "Rerun: sec-inline-financials-lineage-change 2 3" in output


def test_empty_successor_ingestion_rerun_preserves_effective_date(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: CompanyIngestionResult(
            company=Company(
                ticker="NEW",
                cik="0000000003",
                name="New Successor Legal Name",
            ),
            status="checked_no_filings",
            checked_sec=True,
            annual_check_due=True,
            quarterly_check_due=True,
        ),
    )
    output: list[str] = []

    exit_code = run_successor_promotion_command(
        service,
        "2",
        "3",
        effective_date=date(2026, 5, 1),
        input_fn=lambda _prompt: "y",
        output_fn=output.append,
    )

    assert exit_code == 1
    assert "Lineage relationship was retained." in output
    assert "Rerun: sec-inline-financials-lineage-change 2 3 --effective-date 2026-05-01" in output


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


def test_predecessor_command_uses_existing_prompt_and_ingests_predecessor(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    ingested: list[str] = []
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda cik: (
            ingested.append(cik)
            or CompanyIngestionResult(
                company=company,
                status="updated",
                checked_sec=True,
                annual_check_due=True,
                quarterly_check_due=True,
            )
        ),
    )
    prompts: list[str] = []
    output: list[str] = []

    exit_code = run_lineage_command(
        service,
        "2",
        "1",
        input_fn=lambda prompt: prompts.append(prompt) or "y",
        output_fn=output.append,
    )

    assert exit_code == 0
    assert prompts == ["Create this lineage and ingest the predecessor's filings? [y/n]: "]
    assert ingested == ["0000000002"]
    assert "  0000000001 -> 0000000002" in output
    assert "Lineage: created" in output


def test_predecessor_ingestion_failure_and_interrupt_print_exact_rerun(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    failures = iter((DiscoveryError("SEC unavailable"), KeyboardInterrupt()))

    def fail(_cik: str) -> CompanyIngestionResult:
        raise next(failures)

    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=fail,
    )
    first_output: list[str] = []
    second_output: list[str] = []

    first_code = run_lineage_command(
        service,
        "2",
        "1",
        effective_date=date(2026, 5, 1),
        input_fn=lambda _prompt: "y",
        output_fn=first_output.append,
    )
    second_code = run_lineage_command(
        service,
        "2",
        "1",
        effective_date=date(2026, 5, 1),
        input_fn=lambda _prompt: "y",
        output_fn=second_output.append,
    )

    rerun = "Rerun: sec-inline-financials-lineage 2 1 --effective-date 2026-05-01"
    assert first_code == 1
    assert "Error: SEC unavailable" in first_output
    assert rerun in first_output
    assert second_code == 130
    assert "Interrupted after approval; the lineage may already be committed." in second_output
    assert rerun in second_output


@pytest.mark.parametrize("answer", ["n", ""])
def test_cancelled_lineage_confirmation_does_not_mutate_or_ingest(
    tmp_path: Path,
    answer: str,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3")
    store.initialize()
    company = Company(ticker="NEW", cik="0000000002", name="Stored Current Name")
    store.create_processing_run(company, purpose="seed", requested_window={})
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(),
        ingest_company_cik=lambda _cik: pytest.fail("cancelled lineage must not ingest"),
    )
    output: list[str] = []

    exit_code = run_lineage_command(
        service,
        "2",
        "1",
        input_fn=lambda _prompt: answer,
        output_fn=output.append,
    )

    lineage = store.load_company_lineage("2")
    assert exit_code == 0
    assert lineage is not None
    assert lineage.current_to_oldest == (RegistrantIdentity("0000000002", "Stored Current Name"),)
    assert "Lineage cancelled; no lineage or ingestion changes made." in output
