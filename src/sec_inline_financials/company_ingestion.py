from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, timedelta
from pathlib import Path
from typing import Literal

from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.config import load_sec_user_agent
from sec_inline_financials.errors import DiscoveryError, ExplorerError, IngestionError
from sec_inline_financials.evidence_ingestion import EvidenceIngestionService, EvidenceProcessor
from sec_inline_financials.evidence_models import RunOutcome, StoredCompanyState
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.sec_client import SecClient
from sec_inline_financials.service import SecGateway
from sec_inline_financials.storage.config import evidence_runtime_paths
from sec_inline_financials.storage.evidence_store import EvidenceStore

IngestionStatus = Literal[
    "initialized",
    "updated",
    "checked_no_update",
    "reused_local",
    "refresh_failed_using_local_data",
]
_TICKER_PATTERN = re.compile(r"[A-Z0-9.-]{1,10}")


@dataclass(frozen=True)
class IngestionSettings:
    database_path: Path
    artifact_root: Path
    sec_user_agent: str = ""
    annual_count: int = 5
    quarterly_count: int = 12
    cache_directory: Path | None = None
    force_refresh: bool = False

    @classmethod
    def from_environment(
        cls,
        *,
        working_directory: Path | None = None,
        environment: Mapping[str, str] | None = None,
    ) -> IngestionSettings:
        current_environment = os.environ if environment is None else environment
        paths = evidence_runtime_paths(current_environment)
        return cls(
            database_path=paths.database,
            artifact_root=paths.artifacts,
            sec_user_agent=load_sec_user_agent(
                working_directory=working_directory or Path.cwd(),
                environment=current_environment,
            ),
            cache_directory=paths.root / "cache" / "arelle",
        )


@dataclass(frozen=True)
class CompanyIngestionResult:
    company: Company
    status: IngestionStatus
    checked_sec: bool
    annual_check_due: bool
    quarterly_check_due: bool
    new_accessions: tuple[str, ...] = ()
    active_accessions: tuple[str, ...] = ()
    run: RunOutcome | None = None
    error: str | None = None


class CompanyIngestionService:
    """Request-triggered company refresh over immutable filing evidence."""

    def __init__(
        self,
        *,
        store: EvidenceStore,
        sec_gateway_factory: Callable[[], SecGateway],
        processor_factory: Callable[[], EvidenceProcessor],
        today_fn: Callable[[], date] = date.today,
        progress: Callable[[str], None] | None = None,
    ) -> None:
        self._store = store
        self._sec_gateway_factory = sec_gateway_factory
        self._processor_factory = processor_factory
        self._today = today_fn
        self._progress = progress or (lambda _message: None)

    def ingest_company(
        self,
        ticker: str,
        *,
        annual_count: int = 5,
        quarterly_count: int = 12,
        force_refresh: bool = False,
    ) -> CompanyIngestionResult:
        requested = ticker.strip().upper()
        if _TICKER_PATTERN.fullmatch(requested) is None:
            raise IngestionError("Ticker must contain 1-10 letters, digits, dots, or hyphens.")
        if annual_count < 1 or quarterly_count < 1:
            raise IngestionError("Annual and quarterly filing counts must both be positive.")

        self._progress(f"Initializing evidence storage for {requested}...")
        self._store.initialize()
        self._progress(f"Checking stored refresh state for {requested}...")
        today = self._today()
        previous = self._store.get_company_state(requested)
        annual_due = _check_is_due(
            previous.next_check_date_10k if previous is not None else None, today
        )
        quarterly_due = _check_is_due(
            previous.next_check_date_10q if previous is not None else None, today
        )
        requires_evidence = previous is None or not previous.has_evidence
        incomplete_window = bool(
            previous is not None
            and (
                previous.active_filings_without_evidence or previous.active_filings_without_sections
            )
        )
        should_check_sec = (
            force_refresh or requires_evidence or incomplete_window or annual_due or quarterly_due
        )
        if previous is not None and not should_check_sec:
            self._progress(f"Stored evidence is current for {requested}; SEC check skipped.")
            return CompanyIngestionResult(
                company=previous.company,
                status="reused_local",
                checked_sec=False,
                annual_check_due=False,
                quarterly_check_due=False,
                active_accessions=previous.active_accessions,
            )

        try:
            self._progress(f"Checking SEC filings for {requested}...")
            gateway = self._sec_gateway_factory()
            company = gateway.resolve_company(requested)
            if previous is None:
                previous = self._store.get_company_state_by_cik(company.cik)
                if previous is not None:
                    annual_due = _check_is_due(previous.next_check_date_10k, today)
                    quarterly_due = _check_is_due(previous.next_check_date_10q, today)
            if previous is not None and previous.company.cik != company.cik:
                raise IngestionError(
                    f"Stored ticker {requested} belongs to CIK {previous.company.cik}, "
                    f"but SEC resolved it to {company.cik}."
                )
            annual = gateway.discover_annual_inline_filings(company, count=annual_count)
            quarterly = gateway.discover_quarterly_inline_filings(company, count=quarterly_count)
        except DiscoveryError as exc:
            if previous is None or not previous.has_evidence:
                raise
            self._progress(f"SEC refresh failed for {requested}; using stored evidence.")
            return _failed_refresh_result(
                previous,
                annual_due=annual_due,
                quarterly_due=quarterly_due,
                error=exc,
            )

        known_accessions = set(previous.known_accessions if previous is not None else ())
        selected = (*annual, *quarterly)
        self._progress(
            f"Selected filings for {company.ticker}: "
            f"{len(annual)} annual, {len(quarterly)} quarterly."
        )
        self._progress(f"Processing selected filings for {company.ticker}: {len(selected)} total.")
        new_accessions = tuple(
            filing.accession for filing in selected if filing.accession not in known_accessions
        )
        evidence_service = EvidenceIngestionService(
            sec_client=gateway,
            processor=self._processor_factory(),
            store=self._store,
            progress=self._progress,
        )
        run = evidence_service.ingest_selected_window(
            company,
            annual=annual,
            quarterly=quarterly,
        )
        failed = tuple(outcome for outcome in run.filings if outcome.status == "failed")
        if run.status == "failed":
            error = _failure_summary(failed)
            if previous is not None and previous.has_evidence:
                return _failed_refresh_result(
                    previous,
                    annual_due=annual_due,
                    quarterly_due=quarterly_due,
                    error=IngestionError(error),
                    run=run,
                )
            raise IngestionError(f"Initial ingestion produced no usable filing evidence: {error}")

        new_accession_set = set(new_accessions)
        new_forms = {filing.form for filing in selected if filing.accession in new_accession_set}
        failed_accessions = {outcome.accession for outcome in failed}
        failed_forms = {filing.form for filing in selected if filing.accession in failed_accessions}
        latest_annual = max(annual, key=lambda filing: filing.filing_date)
        latest_quarterly = max(quarterly, key=lambda filing: filing.filing_date)
        latest_quarterly_period = self._store.fiscal_period_for_accession(
            latest_quarterly.accession
        )
        next_annual = _next_check_after_refresh(
            previous=previous,
            form="10-K",
            latest_filing=latest_annual,
            fiscal_period=None,
            check_was_due=annual_due,
            discovered_new="10-K" in new_forms,
            checked_on=today,
        )
        next_quarterly = _next_check_after_refresh(
            previous=previous,
            form="10-Q",
            latest_filing=latest_quarterly,
            fiscal_period=latest_quarterly_period,
            check_was_due=quarterly_due,
            discovered_new="10-Q" in new_forms,
            checked_on=today,
        )
        if "10-K" in failed_forms:
            next_annual = today
        if "10-Q" in failed_forms:
            next_quarterly = today
        self._progress(f"Publishing the active filing window for {company.ticker}...")
        self._store.publish_filing_window(
            company,
            annual=annual,
            quarterly=quarterly,
            next_check_date_10k=next_annual,
            next_check_date_10q=next_quarterly,
        )
        current = self._store.get_company_state(company.ticker)
        if current is None:
            raise IngestionError("Company refresh committed without a readable company state.")

        if previous is None or not previous.has_evidence:
            status: IngestionStatus = "initialized"
        elif new_accessions:
            status = "updated"
        else:
            status = "checked_no_update"
        self._progress(f"Completed ingestion for {company.ticker}: {status}.")
        return CompanyIngestionResult(
            company=company,
            status=status,
            checked_sec=True,
            annual_check_due=annual_due,
            quarterly_check_due=quarterly_due,
            new_accessions=new_accessions,
            active_accessions=current.active_accessions,
            run=run,
            error=_failure_summary(failed) if failed else None,
        )


def ingest_company(
    ticker: str,
    settings: IngestionSettings,
    *,
    progress: Callable[[str], None] | None = None,
) -> CompanyIngestionResult:
    """Ingest or update one company, contacting the SEC only when required."""
    store = EvidenceStore(settings.database_path, settings.artifact_root)

    def require_user_agent() -> str:
        if settings.sec_user_agent:
            return settings.sec_user_agent
        raise DiscoveryError(
            "SEC_USER_AGENT is required when a company refresh must contact the SEC."
        )

    service = CompanyIngestionService(
        store=store,
        sec_gateway_factory=lambda: SecClient(user_agent=require_user_agent()),
        processor_factory=lambda: ArelleProcessor(
            user_agent=require_user_agent(),
            cache_directory=settings.cache_directory,
        ),
        progress=progress,
    )
    return service.ingest_company(
        ticker,
        annual_count=settings.annual_count,
        quarterly_count=settings.quarterly_count,
        force_refresh=settings.force_refresh,
    )


def _failed_refresh_result(
    previous: StoredCompanyState,
    *,
    annual_due: bool,
    quarterly_due: bool,
    error: BaseException,
    run: RunOutcome | None = None,
) -> CompanyIngestionResult:
    return CompanyIngestionResult(
        company=previous.company,
        status="refresh_failed_using_local_data",
        checked_sec=True,
        annual_check_due=annual_due,
        quarterly_check_due=quarterly_due,
        active_accessions=previous.active_accessions,
        run=run,
        error=f"{type(error).__name__}: {error}",
    )


def _failure_summary(failures: Sequence[object]) -> str:
    descriptions = [
        f"{getattr(item, 'accession', 'unknown')}: {getattr(item, 'error', 'failed')}"
        for item in failures
    ]
    return "; ".join(descriptions) or "all filing attempts failed"


def _check_is_due(next_check: date | None, today: date) -> bool:
    return next_check is None or today >= next_check


def _next_check_after_refresh(
    *,
    previous: StoredCompanyState | None,
    form: Literal["10-K", "10-Q"],
    latest_filing: Filing,
    fiscal_period: str | None,
    check_was_due: bool,
    discovered_new: bool,
    checked_on: date,
) -> date:
    previous_check = (
        previous.next_check_date_10k
        if previous is not None and form == "10-K"
        else previous.next_check_date_10q
        if previous is not None
        else None
    )
    if previous is None or not previous.has_evidence or discovered_new:
        months = 12 if form == "10-K" else 6 if (fiscal_period or "").upper() == "Q3" else 3
        return _previous_market_day(_add_months(latest_filing.filing_date, months))
    if check_was_due:
        return _next_market_day(checked_on)
    if previous_check is None:
        raise IngestionError(f"Stored {form} refresh state unexpectedly has no next-check date.")
    return previous_check


def _add_months(value: date, months: int) -> date:
    month_index = value.month - 1 + months
    year = value.year + month_index // 12
    month = month_index % 12 + 1
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    last_day = (next_month - timedelta(days=1)).day
    return date(year, month, min(value.day, last_day))


def _previous_market_day(value: date) -> date:
    candidate = value
    while not _is_market_day(candidate):
        candidate -= timedelta(days=1)
    return candidate


def _next_market_day(value: date) -> date:
    candidate = value + timedelta(days=1)
    while not _is_market_day(candidate):
        candidate += timedelta(days=1)
    return candidate


def _is_market_day(value: date) -> bool:
    return value.weekday() < 5 and value not in _market_holidays(value.year)


def _market_holidays(year: int) -> frozenset[date]:
    holidays = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _easter_sunday(year) - timedelta(days=2),
        _last_weekday(year, 5, 0),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    if year >= 2022:
        holidays.add(_observed(date(year, 6, 19)))
    next_new_year = _observed(date(year + 1, 1, 1))
    if next_new_year.year == year:
        holidays.add(next_new_year)
    return frozenset(holidays)


def _observed(value: date) -> date:
    if value.weekday() == 5:
        return value - timedelta(days=1)
    if value.weekday() == 6:
        return value + timedelta(days=1)
    return value


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + 7 * (occurrence - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    next_month = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    candidate = next_month - timedelta(days=1)
    return candidate - timedelta(days=(candidate.weekday() - weekday) % 7)


def _easter_sunday(year: int) -> date:
    a = year % 19
    b, c = divmod(year, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    ell = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * ell) // 451
    month = (h + ell - 7 * m + 114) // 31
    day = (h + ell - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _print_cli_progress(message: str) -> None:
    print(f"[progress] {message}", file=sys.stderr, flush=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest or update retained SEC filing evidence.")
    parser.add_argument("ticker")
    parser.add_argument("--annual-count", type=int, default=5)
    parser.add_argument("--quarterly-count", type=int, default=12)
    parser.add_argument("--force", action="store_true", help="Check SEC even when not yet due.")
    args = parser.parse_args(argv)
    settings = replace(
        IngestionSettings.from_environment(),
        annual_count=args.annual_count,
        quarterly_count=args.quarterly_count,
        force_refresh=args.force,
    )
    try:
        result = ingest_company(args.ticker, settings, progress=_print_cli_progress)
    except ExplorerError as exc:
        print(f"Error: {exc}")
        return 1
    print(f"{result.company.ticker}: {result.status}")
    if result.run is not None:
        print(f"Run: {result.run.run_id} ({result.run.status})")
        for filing in result.run.filings:
            print(f"{filing.accession}: {filing.status}")
    if result.error:
        print(f"Warning: {result.error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
