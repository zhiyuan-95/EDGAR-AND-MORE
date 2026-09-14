from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from sec_inline_financials.models import (
    AnnualResult,
    QuarterlyReportData,
    QuarterlyResult,
    ReportData,
)
from sec_inline_financials.report import render_quarterly_report, render_report
from sec_inline_financials.storage.config import evidence_runtime_paths
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.report_projection import project_stored_report


@dataclass(frozen=True)
class FilingSnapshot:
    snapshot_id: int
    fiscal_year: int
    fiscal_period: str | None
    form: str
    accession: str
    filing_date: str
    report_date: str


def _find_annual_snapshots(database_path: Path, ticker: str) -> tuple[FilingSnapshot, ...]:
    """Return the newest stored 10-K snapshot for each available fiscal year."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                s.id AS snapshot_id,
                COALESCE(
                    s.fiscal_year,
                    CAST(substr(f.report_date, 1, 4) AS INTEGER)
                ) AS fiscal_year,
                s.fiscal_period,
                f.form,
                f.accession,
                f.filing_date,
                f.report_date
            FROM evidence_snapshots AS s
            JOIN filings AS f ON f.id = s.filing_id
            JOIN companies AS c ON c.id = f.company_id
            WHERE upper(c.ticker) = upper(?)
              AND f.form = '10-K'
            ORDER BY fiscal_year DESC, f.report_date DESC, s.id DESC
            """,
            (ticker,),
        ).fetchall()

    newest_by_year: dict[int, FilingSnapshot] = {}
    for row in rows:
        fiscal_year = int(row["fiscal_year"])
        newest_by_year.setdefault(
            fiscal_year,
            FilingSnapshot(
                snapshot_id=int(row["snapshot_id"]),
                fiscal_year=fiscal_year,
                fiscal_period=(
                    str(row["fiscal_period"]) if row["fiscal_period"] is not None else None
                ),
                form=str(row["form"]),
                accession=str(row["accession"]),
                filing_date=str(row["filing_date"]),
                report_date=str(row["report_date"]),
            ),
        )
    return tuple(sorted(newest_by_year.values(), key=lambda item: item.fiscal_year))


def _find_complete_quarterly_years(
    database_path: Path, ticker: str
) -> dict[int, tuple[FilingSnapshot, ...]]:
    """Return fiscal years with newest stored snapshots for each of Q1, Q2, and Q3."""
    with closing(sqlite3.connect(database_path)) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT
                s.id AS snapshot_id,
                s.fiscal_year,
                s.fiscal_period,
                f.form,
                f.accession,
                f.filing_date,
                f.report_date
            FROM evidence_snapshots AS s
            JOIN filings AS f ON f.id = s.filing_id
            JOIN companies AS c ON c.id = f.company_id
            WHERE upper(c.ticker) = upper(?)
              AND f.form = '10-Q'
              AND s.fiscal_year IS NOT NULL
              AND s.fiscal_period IN ('Q1', 'Q2', 'Q3')
            ORDER BY s.fiscal_year, s.fiscal_period, s.id DESC
            """,
            (ticker,),
        ).fetchall()

    newest_by_quarter: dict[tuple[int, str], FilingSnapshot] = {}
    for row in rows:
        fiscal_year = int(row["fiscal_year"])
        fiscal_period = str(row["fiscal_period"])
        newest_by_quarter.setdefault(
            (fiscal_year, fiscal_period),
            FilingSnapshot(
                snapshot_id=int(row["snapshot_id"]),
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                form=str(row["form"]),
                accession=str(row["accession"]),
                filing_date=str(row["filing_date"]),
                report_date=str(row["report_date"]),
            ),
        )

    complete: dict[int, tuple[FilingSnapshot, ...]] = {}
    fiscal_years = sorted({year for year, _period in newest_by_quarter})
    for fiscal_year in fiscal_years:
        quarter_keys = tuple((fiscal_year, period) for period in ("Q1", "Q2", "Q3"))
        if all(key in newest_by_quarter for key in quarter_keys):
            complete[fiscal_year] = tuple(newest_by_quarter[key] for key in quarter_keys)
    return complete


def _parse_year_selection(selection: str, available_years: tuple[int, ...]) -> tuple[int, ...]:
    """Parse values such as '2023,2025', '2021-2024', or 'all'."""
    normalized = selection.strip().lower()
    if normalized == "all":
        return available_years
    if not normalized:
        raise ValueError("Enter at least one fiscal year or 'all'.")

    selected: set[int] = set()
    for token in normalized.split(","):
        token = token.strip()
        if not token:
            raise ValueError("Year selections cannot contain an empty item.")
        if "-" in token:
            start_text, end_text = (part.strip() for part in token.split("-", 1))
            if not start_text.isdigit() or not end_text.isdigit():
                raise ValueError(f"Invalid year range: {token!r}.")
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise ValueError(f"Year range must run from oldest to newest: {token!r}.")
            selected.update(range(start, end + 1))
        elif token.isdigit():
            selected.add(int(token))
        else:
            raise ValueError(f"Invalid fiscal year: {token!r}.")

    unavailable = sorted(selected.difference(available_years))
    if unavailable:
        missing = ", ".join(str(year) for year in unavailable)
        raise ValueError(f"No complete stored data is available for: {missing}.")
    return tuple(sorted(selected))


def _prompt_for_ticker() -> str:
    while True:
        ticker = input("Company ticker: ").strip().upper()
        if ticker:
            return ticker
        print("Please enter a company ticker.")


def _prompt_for_report_type() -> str:
    while True:
        report_type = input("Report type — annual or quarterly? [A/Q]: ").strip().lower()
        if report_type in {"a", "annual"}:
            return "annual"
        if report_type in {"q", "quarterly"}:
            return "quarterly"
        print("Please enter A for annual or Q for quarterly.")


def _prompt_for_years(available_years: tuple[int, ...]) -> tuple[int, ...]:
    while True:
        selection = input("Fiscal years (comma-separated, a range, or 'all'): ")
        try:
            return _parse_year_selection(selection, available_years)
        except ValueError as exc:
            print(f"Invalid selection: {exc}")


def _load_all_facts(store: EvidenceStore, snapshot_id: int) -> tuple[dict[str, object], ...]:
    facts: list[dict[str, object]] = []
    cursor = 0
    while True:
        page = store.list_facts(snapshot_id=snapshot_id, cursor=cursor, limit=1000)
        facts.extend(page.items)
        if page.next_cursor is None:
            return tuple(facts)
        cursor = page.next_cursor


def _build_annual_report(
    store: EvidenceStore,
    ticker: str,
    snapshots: tuple[FilingSnapshot, ...],
) -> str:
    company_state = store.get_company_state(ticker)
    if company_state is None:
        raise RuntimeError(f"No stored company data found for {ticker}.")

    annual_results: list[AnnualResult] = []
    for snapshot in snapshots:
        evaluation = store.get_report_evaluation(
            snapshot.snapshot_id,
            "annual",
            "report-v1",
        )
        if evaluation is None:
            raise RuntimeError(
                f"Snapshot {snapshot.snapshot_id} has no annual report-v1 evaluation."
            )
        projected = project_stored_report(
            store,
            snapshot.snapshot_id,
            evaluation.evaluation_id,
        )
        if not isinstance(projected, AnnualResult):
            raise RuntimeError(f"Snapshot {snapshot.snapshot_id} is not an annual report.")
        annual_results.append(projected)

    return render_report(
        ReportData(company=company_state.company, annual_results=tuple(annual_results))
    )


def _build_quarterly_report(
    store: EvidenceStore,
    ticker: str,
    snapshots: tuple[FilingSnapshot, ...],
) -> str:
    company_state = store.get_company_state(ticker)
    if company_state is None:
        raise RuntimeError(f"No stored company data found for {ticker}.")

    quarterly_results: list[QuarterlyResult] = []
    for snapshot in snapshots:
        evaluation = store.get_report_evaluation(
            snapshot.snapshot_id,
            "quarterly",
            "report-v1",
        )
        if evaluation is None:
            raise RuntimeError(
                f"Snapshot {snapshot.snapshot_id} has no quarterly report-v1 evaluation."
            )
        projected = project_stored_report(
            store,
            snapshot.snapshot_id,
            evaluation.evaluation_id,
        )
        if not isinstance(projected, QuarterlyResult):
            raise RuntimeError(f"Snapshot {snapshot.snapshot_id} is not a quarterly report.")
        quarterly_results.append(projected)

    return render_quarterly_report(
        QuarterlyReportData(
            company=company_state.company,
            quarterly_results=tuple(quarterly_results),
        )
    )


def main() -> int:
    try:
        paths = evidence_runtime_paths(os.environ)
        if not paths.database.is_file():
            raise RuntimeError(f"Evidence database does not exist: {paths.database.resolve()}")

        ticker = _prompt_for_ticker()
        report_type = _prompt_for_report_type()
        if report_type == "annual":
            available_snapshots = _find_annual_snapshots(paths.database, ticker)
            if not available_snapshots:
                raise RuntimeError(f"No stored 10-K snapshots found for {ticker}.")
            available_years = tuple(item.fiscal_year for item in available_snapshots)
            print("Available annual fiscal years: " + ", ".join(map(str, available_years)))
        else:
            quarterly_by_year = _find_complete_quarterly_years(paths.database, ticker)
            if not quarterly_by_year:
                raise RuntimeError(
                    f"No fiscal year with stored Q1, Q2, and Q3 10-Q snapshots found for {ticker}."
                )
            available_years = tuple(quarterly_by_year)
            print(
                "Available quarterly fiscal years with Q1, Q2, and Q3: "
                + ", ".join(map(str, available_years))
            )

        selected_years = _prompt_for_years(available_years)
        if report_type == "annual":
            selected = tuple(
                snapshot
                for snapshot in available_snapshots
                if snapshot.fiscal_year in selected_years
            )
        else:
            selected = tuple(
                snapshot
                for fiscal_year in selected_years
                for snapshot in quarterly_by_year[fiscal_year]
            )

        store = EvidenceStore(paths.database, paths.artifacts)
        facts_by_snapshot = {
            snapshot.snapshot_id: _load_all_facts(store, snapshot.snapshot_id)
            for snapshot in selected
        }
        if report_type == "annual":
            report_text = _build_annual_report(store, ticker, selected)
        else:
            report_text = _build_quarterly_report(store, ticker, selected)

        output_directory = Path("output")
        output_directory.mkdir(parents=True, exist_ok=True)
        year_slug = "_".join(str(year) for year in selected_years)
        output_stem = f"{ticker}_{report_type}_{year_slug}"
        report_path = output_directory / f"{output_stem}.txt"
        json_path = output_directory / f"{output_stem}_all_facts.json"

        report_path.write_text(report_text, encoding="utf-8")
        json_payload = {
            "ticker": ticker,
            "report_type": report_type,
            "fiscal_years": selected_years,
            "filings": [
                {
                    "snapshot_id": snapshot.snapshot_id,
                    "fiscal_year": snapshot.fiscal_year,
                    "fiscal_period": snapshot.fiscal_period,
                    "form": snapshot.form,
                    "accession": snapshot.accession,
                    "filing_date": snapshot.filing_date,
                    "report_date": snapshot.report_date,
                    "facts": facts_by_snapshot[snapshot.snapshot_id],
                }
                for snapshot in selected
            ],
        }
        json_path.write_text(
            json.dumps(json_payload, indent=2, default=str),
            encoding="utf-8",
        )

        fact_count = sum(len(facts) for facts in facts_by_snapshot.values())
        print(
            f"Retrieved {fact_count} fact occurrences across {len(selected)} "
            f"{report_type} filing(s)."
        )
        print(f"TXT report: {report_path.resolve()}")
        print(f"Complete JSON data: {json_path.resolve()}")
        return 0
    except (EOFError, KeyboardInterrupt):
        print("\nCancelled.")
        return 130
    except (OSError, RuntimeError, sqlite3.Error, ValueError) as exc:
        print(f"Error: {exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
