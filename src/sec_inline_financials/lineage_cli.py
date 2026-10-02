from __future__ import annotations

import argparse
import os
from collections.abc import Callable, Sequence
from datetime import date

from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.company_ingestion import CompanyIngestionService, IngestionSettings
from sec_inline_financials.company_lineage import (
    LineageMaintenanceService,
    LineagePatchPlan,
    normalize_cik,
)
from sec_inline_financials.errors import ExplorerError, SchemaError
from sec_inline_financials.sec_client import SecClient
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.migrations import inspect_database_schema

InputFn = Callable[[str], str]
OutputFn = Callable[[str], None]


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("expected an ISO date in YYYY-MM-DD form") from exc


def _render_plan(plan: LineagePatchPlan, output_fn: OutputFn) -> None:
    canonical = plan.before.current_to_oldest[0]
    output_fn("Canonical company")
    output_fn(f"  {canonical.legal_name} (current CIK {plan.before.canonical_current_cik})")
    output_fn("")
    output_fn("Existing lineage")
    for member in plan.before.current_to_oldest:
        name = plan.successor.legal_name if member.cik == plan.successor.cik else member.legal_name
        output_fn(f"  {member.cik}  {name}")
    output_fn("")
    output_fn("Proposed direct edge")
    output_fn(f"  {plan.predecessor.cik}  {plan.predecessor.legal_name}")
    output_fn("       ->")
    output_fn(f"  {plan.successor.cik}  {plan.successor.legal_name}")
    output_fn(f"  {plan.predecessor.cik} -> {plan.successor.cik}")
    edge = next(
        edge
        for edge in plan.after.edges
        if edge.predecessor_cik == plan.predecessor.cik and edge.successor_cik == plan.successor.cik
    )
    if edge.effective_date is not None:
        output_fn(f"  Effective date: {edge.effective_date.isoformat()}")
    output_fn("  Status: already present" if plan.already_present else "  Status: new edge")


def run_lineage_command(
    service: LineageMaintenanceService,
    successor_cik: str,
    predecessor_cik: str,
    *,
    effective_date: date | None = None,
    input_fn: InputFn = input,
    output_fn: OutputFn = print,
) -> int:
    try:
        plan = service.preview_link(
            successor_cik,
            predecessor_cik,
            effective_date=effective_date,
        )
        _render_plan(plan, output_fn)
        while True:
            try:
                answer = input_fn(
                    "Create this lineage and ingest the predecessor's filings? [y/n]: "
                ).strip()
            except EOFError:
                answer = ""
            if answer.lower() == "y":
                break
            if answer.lower() in {"", "n"}:
                output_fn("Lineage cancelled; no lineage or ingestion changes made.")
                return 0
            output_fn("Please enter y or n.")
    except KeyboardInterrupt:
        output_fn("Lineage cancelled; no lineage or ingestion changes made.")
        return 130
    except ExplorerError as exc:
        output_fn(f"Error: {exc}")
        return 1

    try:
        result = service.apply_and_ingest(plan)
    except KeyboardInterrupt:
        output_fn("Interrupted after approval; the lineage may already be committed.")
        output_fn("Rerun this same command to retry ingestion safely.")
        return 130
    except ExplorerError as exc:
        output_fn(f"Error: {exc}")
        output_fn("If the lineage was committed before ingestion failed, rerun this same command.")
        return 1

    output_fn(f"Lineage: {result.disposition}")
    ingestion_status = getattr(result.ingestion, "status", None)
    if ingestion_status is not None:
        output_fn(f"Ingestion: {ingestion_status}")
    else:
        output_fn("Ingestion: completed")
    ingestion_error = getattr(result.ingestion, "error", None)
    if ingestion_error:
        output_fn(f"Ingestion warning: {ingestion_error}")
        output_fn("Rerun this same command to retry failed filing ingestion safely.")
        return 1
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Link an exact predecessor CIK and ingest its filings."
    )
    parser.add_argument("successor_cik")
    parser.add_argument("predecessor_cik")
    parser.add_argument(
        "--effective-date",
        type=_iso_date,
        help="verified legal transition date (YYYY-MM-DD)",
    )
    args = parser.parse_args(argv)
    try:
        successor_cik = normalize_cik(args.successor_cik)
        predecessor_cik = normalize_cik(args.predecessor_cik)
        settings = IngestionSettings.from_environment(environment=os.environ)
        store = EvidenceStore(settings.database_path, settings.artifact_root)
        schema = inspect_database_schema(store.database)

        if schema.newer_than_code:
            raise SchemaError(
                f"Database schema version {schema.current_version} is newer than "
                f"supported version {schema.supported_version}."
            )

        if schema.pending:
            print(
                f"Database schema version {schema.current_version} must be upgraded "
                f"to version {schema.supported_version}."
            )
            print("Pending migrations:")
            for filename in schema.pending_filenames:
                print(f"  - {filename}")
            print("Warning: older code may reject the upgraded database.")

            while True:
                try:
                    answer = input("Apply these database migrations? [y/n]: ").strip().lower()
                except EOFError:
                    answer = ""
                except KeyboardInterrupt:
                    print("\nMigration cancelled; database unchanged.")
                    return 130

                if answer == "y":
                    store.initialize()
                    print(f"Database upgraded to version {schema.supported_version}.")
                    break

                if answer in {"", "n"}:
                    print("Migration cancelled; database unchanged.")
                    return 0

                print("Please enter y or n.")

        sec_client = SecClient(user_agent=settings.sec_user_agent)
        ingestion_service = CompanyIngestionService(
            store=store,
            sec_gateway_factory=lambda: sec_client,
            processor_factory=lambda: ArelleProcessor(
                user_agent=settings.sec_user_agent,
                cache_directory=settings.cache_directory,
            ),
            progress=lambda message: print(f"[progress] {message}"),
        )
        service = LineageMaintenanceService(
            store=store,
            registrant_resolver=sec_client,
            ingest_company_cik=lambda cik: ingestion_service.ingest_company_cik(
                cik,
                annual_count=settings.annual_count,
                quarterly_count=settings.quarterly_count,
            ),
        )
        return run_lineage_command(
            service,
            successor_cik,
            predecessor_cik,
            effective_date=args.effective_date,
        )
    except ExplorerError as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
