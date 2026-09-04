from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

from sec_inline_financials.errors import ExplorerError

GenerateReports = Callable[[str, int, Path], tuple[Path, Path]]
Input = Callable[[str], str]
Output = Callable[[str], None]

_TICKER_PATTERN = re.compile(r"[A-Z0-9.-]{1,10}")


def run_interactive(
    *,
    input_fn: Input,
    output_fn: Output,
    generate_reports: GenerateReports,
    output_dir: Path,
) -> int:
    """Collect a request at the CLI seam and generate both TXT artifacts."""
    ticker = input_fn("Company ticker: ").strip().upper()
    if _TICKER_PATTERN.fullmatch(ticker) is None:
        output_fn("Error: ticker must contain 1-10 letters, digits, dots, or hyphens.")
        return 2

    raw_years = input_fn("Number of latest annual fiscal years: ").strip()
    try:
        years = int(raw_years)
    except ValueError:
        output_fn("Error: year count must be a whole number from 1 to 20.")
        return 2
    if not 1 <= years <= 20:
        output_fn("Error: year count must be a whole number from 1 to 20.")
        return 2

    try:
        annual_path, quarterly_path = generate_reports(ticker, years, output_dir)
    except ExplorerError as exc:
        output_fn(f"Error: {exc}")
        return 1
    output_fn(f"Annual report saved to: {annual_path}")
    output_fn(f"Quarterly report saved to: {quarterly_path}")
    return 0


def main() -> int:
    """Run the interactive command-line application."""
    from sec_inline_financials.service import generate_reports

    return run_interactive(
        input_fn=input,
        output_fn=print,
        generate_reports=generate_reports,
        output_dir=Path.cwd() / "output",
    )
