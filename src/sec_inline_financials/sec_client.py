from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from datetime import date
from typing import Any

from sec_inline_financials.errors import DiscoveryError
from sec_inline_financials.models import Company, Filing

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_ROOT = "https://data.sec.gov/submissions"
_ARCHIVES_ROOT = "https://www.sec.gov/Archives/edgar/data"

JsonFetcher = Callable[[str, dict[str, str]], Any]
FilingSelector = Callable[[list[Filing]], list[Filing]]


def _default_fetch_json(url: str, headers: dict[str, str]) -> Any:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except (
        urllib.error.HTTPError,
        urllib.error.URLError,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise DiscoveryError(f"SEC request failed for {url}: {exc}") from exc


class SecClient:
    """Small SEC submissions client used only for discovery, never for fact values."""

    def __init__(self, *, user_agent: str, fetch_json: JsonFetcher = _default_fetch_json) -> None:
        email = re.search(r"[^@\s]+@[^@\s]+\.[^@\s]+", user_agent)
        identity = user_agent.replace(email.group(0), "").strip(" ()<>-") if email else ""
        if email is None or len(identity) < 2:
            raise DiscoveryError(
                "SEC_USER_AGENT must include a name or organization and a contact email."
            )
        self._headers = {"User-Agent": user_agent, "Accept": "application/json"}
        self._fetch_json = fetch_json

    def resolve_company(self, ticker: str) -> Company:
        payload = self._fetch_json(_TICKERS_URL, self._headers)
        if not isinstance(payload, Mapping):
            raise DiscoveryError("SEC ticker response has an unexpected structure.")
        requested = ticker.strip().upper()
        for record in payload.values():
            if not isinstance(record, Mapping):
                continue
            if str(record.get("ticker", "")).upper() != requested:
                continue
            try:
                cik = f"{int(record['cik_str']):010d}"
                name = str(record["title"])
            except (KeyError, TypeError, ValueError) as exc:
                raise DiscoveryError(f"SEC ticker record for {requested} is incomplete.") from exc
            return Company(ticker=requested, cik=cik, name=name)
        raise DiscoveryError(f"Ticker {requested} was not found in the SEC ticker file.")

    def discover_annual_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        selected = self._discover_inline_filings(
            company,
            form="10-K",
            count=count,
            select=_latest_by_fiscal_year,
        )
        if len(selected) < count:
            raise DiscoveryError(
                f"Requested {count} annual Inline XBRL 10-K fiscal years, "
                f"but SEC history has {len(selected)}."
            )
        return selected[:count]

    def discover_quarterly_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        selected = self._discover_inline_filings(
            company,
            form="10-Q",
            count=count,
            select=_latest_by_report_date,
        )
        if len(selected) < count:
            raise DiscoveryError(
                f"Requested {count} quarterly Inline XBRL 10-Q filings, "
                f"but SEC history has {len(selected)}."
            )
        return selected[:count]

    def _discover_inline_filings(
        self,
        company: Company,
        *,
        form: str,
        count: int,
        select: FilingSelector,
    ) -> list[Filing]:
        submissions_url = f"{_SUBMISSIONS_ROOT}/CIK{company.cik}.json"
        payload = self._fetch_json(submissions_url, self._headers)
        if not isinstance(payload, Mapping):
            raise DiscoveryError("SEC submissions response has an unexpected structure.")
        filings_block = payload.get("filings")
        if not isinstance(filings_block, Mapping):
            raise DiscoveryError("SEC submissions response does not contain filing metadata.")

        candidates: list[Filing] = []
        recent = filings_block.get("recent")
        if isinstance(recent, Mapping):
            candidates.extend(self._parse_columns(company, recent, form=form))

        history_files = filings_block.get("files", [])
        if isinstance(history_files, list):
            for history_file in history_files:
                if len(select(candidates)) >= count:
                    break
                if not isinstance(history_file, Mapping) or not history_file.get("name"):
                    continue
                history_url = f"{_SUBMISSIONS_ROOT}/{history_file['name']}"
                history = self._fetch_json(history_url, self._headers)
                if isinstance(history, Mapping):
                    candidates.extend(self._parse_columns(company, history, form=form))
        return select(candidates)

    @staticmethod
    def _parse_columns(
        company: Company, columns: Mapping[str, Any], *, form: str = "10-K"
    ) -> list[Filing]:
        accessions = columns.get("accessionNumber", [])
        if not isinstance(accessions, list):
            return []
        filings: list[Filing] = []
        for index, accession_value in enumerate(accessions):
            try:
                filing_form = str(columns["form"][index])
                inline = columns["isInlineXBRL"][index]
                if filing_form != form or inline not in (1, True, "1"):
                    continue
                accession = str(accession_value)
                filing_date = date.fromisoformat(str(columns["filingDate"][index]))
                report_date = date.fromisoformat(str(columns["reportDate"][index]))
                primary_document = str(columns["primaryDocument"][index])
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            accession_path = accession.replace("-", "")
            url = f"{_ARCHIVES_ROOT}/{int(company.cik)}/{accession_path}/{primary_document}"
            filings.append(
                Filing(
                    accession=accession,
                    filing_date=filing_date,
                    report_date=report_date,
                    form=filing_form,
                    primary_document=primary_document,
                    url=url,
                )
            )
        return filings


def _latest_by_fiscal_year(candidates: list[Filing]) -> list[Filing]:
    selected: dict[int, Filing] = {}
    for filing in sorted(candidates, key=lambda item: item.filing_date, reverse=True):
        selected.setdefault(filing.report_date.year, filing)
    return sorted(selected.values(), key=lambda item: item.report_date, reverse=True)


def _latest_by_report_date(candidates: list[Filing]) -> list[Filing]:
    selected: dict[date, Filing] = {}
    for filing in sorted(candidates, key=lambda item: item.filing_date, reverse=True):
        selected.setdefault(filing.report_date, filing)
    return sorted(selected.values(), key=lambda item: item.report_date, reverse=True)
