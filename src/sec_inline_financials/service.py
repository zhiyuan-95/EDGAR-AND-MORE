from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import Protocol

from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.config import load_sec_user_agent
from sec_inline_financials.errors import DiscoveryError, ProcessingError
from sec_inline_financials.models import (
    AnnualResult,
    Company,
    Filing,
    QuarterlyReportData,
    QuarterlyResult,
    ReportData,
)
from sec_inline_financials.report import render_quarterly_report, render_report
from sec_inline_financials.sec_client import SecClient

Progress = Callable[[str], None]


class SecGateway(Protocol):
    def resolve_company(self, ticker: str) -> Company: ...

    def discover_annual_inline_filings(self, company: Company, *, count: int) -> list[Filing]: ...

    def discover_quarterly_inline_filings(
        self, company: Company, *, count: int
    ) -> list[Filing]: ...


class FilingProcessor(Protocol):
    def process(self, filing: Filing) -> AnnualResult: ...

    def process_quarterly(self, filing: Filing) -> QuarterlyResult: ...


class ReportApplication:
    def __init__(
        self,
        *,
        sec_client: SecGateway,
        processor: FilingProcessor,
        progress_fn: Progress,
    ) -> None:
        self._sec_client = sec_client
        self._processor = processor
        self._progress = progress_fn

    def create_report(self, ticker: str, years: int, output_dir: Path) -> Path:
        filing_word = "filing" if years == 1 else "filings"
        self._progress(
            f"Resolving {ticker} and discovering {years} annual Inline XBRL {filing_word}..."
        )
        company = self._sec_client.resolve_company(ticker)
        return self._create_annual_report(company, years, output_dir)

    def _create_annual_report(self, company: Company, years: int, output_dir: Path) -> Path:
        filings = self._sec_client.discover_annual_inline_filings(company, count=years)
        results: list[AnnualResult] = []
        for index, filing in enumerate(filings, start=1):
            self._progress(
                f"Processing fiscal year {filing.report_date.year} ({filing.accession}) "
                f"with Arelle [{index}/{len(filings)}]..."
            )
            results.append(self._processor.process(filing))

        self._progress("Writing structured report...")
        text = render_report(ReportData(company=company, annual_results=tuple(results)))
        unit = "year" if years == 1 else "years"
        path = output_dir / f"{company.ticker}_latest_{years}_{unit}.txt"
        return self._write_report(text, path)

    def create_reports(
        self, ticker: str, *, years: int, quarters: int, output_dir: Path
    ) -> tuple[Path, Path]:
        annual_filing_word = "filing" if years == 1 else "filings"
        self._progress(
            f"Resolving {ticker} and discovering {years} annual Inline XBRL {annual_filing_word}..."
        )
        company = self._sec_client.resolve_company(ticker)
        annual_path = self._create_annual_report(company, years, output_dir)
        filing_word = "filing" if quarters == 1 else "filings"
        self._progress(
            f"Discovering {quarters} quarterly Inline XBRL 10-Q {filing_word} for {ticker}..."
        )
        filings = self._sec_client.discover_quarterly_inline_filings(company, count=quarters)
        results: list[QuarterlyResult] = []
        for index, filing in enumerate(filings, start=1):
            self._progress(
                f"Processing 10-Q ended {filing.report_date.isoformat()} ({filing.accession}) "
                f"with Arelle [{index}/{len(filings)}]..."
            )
            results.append(self._processor.process_quarterly(filing))

        self._progress("Writing quarterly structured report...")
        text = render_quarterly_report(
            QuarterlyReportData(company=company, quarterly_results=tuple(results))
        )
        unit = "quarter" if quarters == 1 else "quarters"
        path = output_dir / f"{company.ticker}_latest_{quarters}_10q_{unit}.txt"
        return annual_path, self._write_report(text, path)

    @staticmethod
    def _write_report(text: str, path: Path) -> Path:
        temporary_path = path.with_suffix(".txt.tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary_path.write_text(text, encoding="utf-8")
            temporary_path.replace(path)
        except OSError as exc:
            raise ProcessingError(f"Could not write report to {path}: {exc}") from exc
        return path


def generate_report(ticker: str, years: int, output_dir: Path) -> Path:
    return _configured_application(output_dir).create_report(ticker, years, output_dir)


def generate_reports(ticker: str, output_dir: Path) -> tuple[Path, Path]:
    return _configured_application(output_dir).create_reports(
        ticker, years=5, quarters=12, output_dir=output_dir
    )


def _configured_application(output_dir: Path) -> ReportApplication:
    user_agent = load_sec_user_agent(working_directory=Path.cwd(), environment=os.environ)
    if not user_agent:
        raise DiscoveryError(
            "SEC_USER_AGENT is not set in the process environment, config.env, or config.txt. "
            "Use a value such as 'Your Name your.email@example.com'."
        )
    return ReportApplication(
        sec_client=SecClient(user_agent=user_agent),
        processor=ArelleProcessor(
            user_agent=user_agent,
            cache_directory=output_dir.parent / ".cache" / "arelle",
        ),
        progress_fn=print,
    )
