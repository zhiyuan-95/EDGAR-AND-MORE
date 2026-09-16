from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping, Sequence

from sec_inline_financials.mapping_models import ReportKind
from sec_inline_financials.storage.config import evidence_runtime_paths
from sec_inline_financials.storage.evidence_store import EvidenceStore


def _render_ascii_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> str:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, value in enumerate(row):
            widths[index] = max(widths[index], len(value))

    def render_row(row: Sequence[str]) -> str:
        return " | ".join(value.ljust(widths[index]) for index, value in enumerate(row))

    divider = "-+-".join("-" * width for width in widths)
    return "\n".join((render_row(headers), divider, *(render_row(row) for row in rows)))


def render_mapping_table(rows: Sequence[Mapping[str, object]]) -> str:
    """Render one Metric Evaluation with metrics as rows and periods as columns."""
    periods = sorted({str(row["report_date"]) for row in rows})
    metric_names = tuple(dict.fromkeys(str(row["display_name"]) for row in rows))
    values: dict[tuple[str, str], str] = {}

    for row in rows:
        metric_name = str(row["display_name"])
        period = str(row["report_date"])
        key = (metric_name, period)
        if key in values:
            raise RuntimeError(f"Duplicate Metric Result for {metric_name} and {period}.")
        value = row.get("typed_value_text")
        values[key] = (
            str(value) if row.get("status") == "reported" and value is not None else "missing"
        )

    table_rows = [
        (metric_name, *(values.get((metric_name, period), "missing") for period in periods))
        for metric_name in metric_names
    ]
    return _render_ascii_table(("Target metric", *periods), table_rows)


def render_metric_summary(rows: Sequence[Mapping[str, object]]) -> str:
    """Summarize mapped concepts and missing periods for every Target Metric."""
    metric_names = tuple(dict.fromkeys(str(row["display_name"]) for row in rows))
    summary_rows: list[tuple[str, str, str]] = []

    for metric_name in metric_names:
        metric_rows = [row for row in rows if str(row["display_name"]) == metric_name]
        concepts = tuple(
            dict.fromkeys(
                str(row["display_qname"])
                for row in metric_rows
                if row.get("status") == "reported" and row.get("display_qname") is not None
            )
        )
        missing_periods = tuple(
            str(row["report_date"]) for row in metric_rows if row.get("status") != "reported"
        )
        summary_rows.append(
            (
                metric_name,
                ", ".join(concepts) if concepts else "none",
                ", ".join(missing_periods) if missing_periods else "none",
            )
        )

    return _render_ascii_table(
        ("Target metric", "Mapped concept(s)", "Missing period(s)"),
        summary_rows,
    )


def _prompt_for_ticker() -> str:
    while True:
        ticker = input("Company ticker: ").strip().upper()
        if ticker:
            return ticker
        print("Please enter a company ticker.")


def _prompt_for_report_kind() -> ReportKind:
    while True:
        selection = input("Mapping to inspect - annual or quarterly? [A/Q]: ").strip().lower()
        if selection in {"a", "annual"}:
            return "annual"
        if selection in {"q", "quarterly"}:
            return "quarterly"
        print("Please enter A for annual or Q for quarterly.")


def main() -> int:
    try:
        paths = evidence_runtime_paths(os.environ)
        if not paths.database.is_file():
            raise RuntimeError(f"Evidence database does not exist: {paths.database.resolve()}")

        ticker = _prompt_for_ticker()
        report_kind = _prompt_for_report_kind()
        store = EvidenceStore(paths.database, paths.artifacts)
        evaluation = store.get_published_metric_evaluation(ticker, report_kind)
        if evaluation is None:
            raise RuntimeError(f"No published {report_kind} mapping found for {ticker}.")

        rows = store.list_metric_results(evaluation.evaluation_id)
        if not rows:
            raise RuntimeError(f"Direct Mapping evaluation {evaluation.evaluation_id} is empty.")

        print(f"\n{ticker} {report_kind} Direct Mapping")
        print(
            f"Evaluation {evaluation.evaluation_id} | "
            f"{evaluation.reported_count} reported | "
            f"{evaluation.missing_count} missing | stale={evaluation.stale}"
        )
        print("\nMapped facts")
        print(render_mapping_table(rows))
        print("\nMetric summary")
        print(render_metric_summary(rows))
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 130
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
