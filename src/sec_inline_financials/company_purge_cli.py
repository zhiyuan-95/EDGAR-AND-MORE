# Preview one company
#uv run --no-sync sec-inline-financials-purge AAPL

# Preview a set
#uv run --no-sync sec-inline-financials-purge AAPL MSFT NVDA

# Permanently execute
#uv run --no-sync sec-inline-financials-purge AAPL MSFT NVDA --execute

# Retry files previously locked by Windows
#uv run --no-sync sec-inline-financials-purge --cleanup-pending


from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from sec_inline_financials.company_purge import (
    CompanyDataPurger,
    CompanyPurgePlan,
    FileCleanupReport,
)
from sec_inline_financials.errors import ExplorerError
from sec_inline_financials.storage.config import evidence_runtime_paths
from sec_inline_financials.storage.evidence_store import EvidenceStore


def _format_bytes(value: int) -> str:
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            return f"{size:.1f} {unit}" if unit != "B" else f"{value} B"
        size /= 1024
    raise AssertionError("unreachable")


def _print_plan(plan: CompanyPurgePlan) -> None:
    print("Companies:")
    for target in plan.targets:
        print(f"  {target.ticker} | CIK {target.cik} | {target.name}")
    print(f"Filings: {plan.filing_count}")
    print(f"Evidence snapshots: {plan.snapshot_count}")
    print(f"Processing runs / attempts: {plan.processing_run_count} / {plan.filing_attempt_count}")
    print(
        "Exclusive artifact files: "
        f"{plan.artifact_file_count} ({_format_bytes(plan.artifact_bytes)})"
    )
    print(f"Company staging/cache directories: {plan.staging_directory_count}")
    print(f"Shared artifact files preserved: {plan.shared_artifact_count}")
    if plan.pending_file_count:
        print(f"Previously pending file deletions: {plan.pending_file_count}")
    if plan.active_ingestion_count:
        print(f"Active ingestion runs blocking execution: {plan.active_ingestion_count}")


def _print_cleanup(report: FileCleanupReport) -> None:
    print(f"Files/directories removed: {len(report.deleted_paths)}")
    if report.missing_paths:
        print(f"Already absent: {len(report.missing_paths)}")
    if report.retained_paths:
        print(f"Re-referenced paths preserved: {len(report.retained_paths)}")
    if report.failures:
        print(f"Pending cleanup failures: {len(report.failures)}")
        for failure in report.failures:
            print(f"  {failure.relative_path}: {failure.error}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Preview or purge all stored evidence owned by one or more company tickers. "
            "The default is a non-deleting preview."
        )
    )
    parser.add_argument(
        "tickers",
        nargs="*",
        help="One or more tickers, separated by spaces or commas (for example: AAPL MSFT).",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Commit the displayed company purge. Without this flag, nothing is deleted.",
    )
    parser.add_argument(
        "--cleanup-pending",
        action="store_true",
        help="Retry file cleanup already committed by an earlier purge.",
    )
    args = parser.parse_args(argv)
    if args.cleanup_pending and (args.tickers or args.execute):
        parser.error("--cleanup-pending cannot be combined with tickers or --execute")
    if not args.cleanup_pending and not args.tickers:
        parser.error("provide at least one ticker, or use --cleanup-pending")

    paths = evidence_runtime_paths(os.environ)
    store = EvidenceStore(paths.database, paths.artifacts)
    purger = CompanyDataPurger(store)
    try:
        if args.cleanup_pending:
            cleanup = purger.cleanup_pending_files()
            _print_cleanup(cleanup)
            return 1 if cleanup.failures else 0

        plan = purger.preview(args.tickers)
        _print_plan(plan)
        if not args.execute:
            print("Dry run only. Re-run with --execute to permanently delete this company data.")
            return 0

        result = purger.purge(args.tickers)
    except (ExplorerError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1

    print("Database purge committed.")
    _print_cleanup(result.cleanup)
    return 1 if result.cleanup.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
