from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from typing import Any

from sec_inline_financials.company_lineage import (
    CompanyLineage,
    RegistrantIdentity,
    normalize_cik,
)
from sec_inline_financials.errors import DiscoveryError, LineageError
from sec_inline_financials.models import Company, Filing

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_ROOT = "https://data.sec.gov/submissions"
_ARCHIVES_ROOT = "https://www.sec.gov/Archives/edgar/data"
_FILING_COLUMN_NAMES = (
    "accessionNumber",
    "form",
    "isInlineXBRL",
    "filingDate",
    "reportDate",
    "primaryDocument",
)

JsonFetcher = Callable[[str, dict[str, str]], Any]
FilingSelector = Callable[[list[Filing]], list[Filing]]


@dataclass(frozen=True)
class DiscoveredCompanyWindow:
    annual: tuple[Filing, ...]
    quarterly: tuple[Filing, ...]


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
        self._json_cache: dict[str, Any] = {}

    def _get_json(self, url: str) -> Any:
        if url not in self._json_cache:
            self._json_cache[url] = self._fetch_json(url, self._headers)
        return self._json_cache[url]

    def resolve_company(self, ticker: str) -> Company:
        payload = self._get_json(_TICKERS_URL)
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

    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        requested = normalize_cik(cik)
        payload = self._get_json(f"{_SUBMISSIONS_ROOT}/CIK{requested}.json")
        if not isinstance(payload, Mapping):
            raise DiscoveryError("SEC submissions response has an unexpected structure.")
        try:
            response_cik = normalize_cik(str(payload["cik"]))
        except (KeyError, TypeError, ValueError, LineageError) as exc:
            raise DiscoveryError(
                f"SEC submissions response for CIK {requested} has no valid CIK."
            ) from exc
        if response_cik != requested:
            raise DiscoveryError(
                f"SEC submissions response CIK {response_cik} does not match {requested}."
            )
        legal_name = str(payload.get("name", "")).strip()
        if not legal_name:
            raise DiscoveryError(f"SEC submissions response for CIK {requested} has no name.")
        return RegistrantIdentity(cik=requested, legal_name=legal_name)

    def discover_company_window(
        self,
        lineage: CompanyLineage,
        *,
        annual_count: int,
        quarterly_count: int,
    ) -> DiscoveredCompanyWindow:
        if annual_count < 1 or quarterly_count < 1:
            raise DiscoveryError("Annual and quarterly filing counts must both be positive.")
        annual_slots: dict[int, Filing] = {}
        quarterly_slots: dict[date, Filing] = {}
        used_accessions: set[str] = set()

        # Current A -> predecessor B -> older C. Fill A's slots first; B and C
        # can fill gaps only, never overwrite a higher-priority period.
        for identity in lineage.current_to_oldest:
            company = Company(ticker="", cik=identity.cik, name=identity.legal_name)
            payload = self._submissions_payload(identity.cik)
            filings_block = payload.get("filings")
            if not isinstance(filings_block, Mapping):
                raise DiscoveryError(
                    f"SEC submissions response for CIK {identity.cik} has no filings block."
                )
            candidates: list[Filing] = []
            recent = filings_block.get("recent")
            if isinstance(recent, Mapping):
                candidates.extend(self._parse_requested_columns(company, recent))
            history_files = filings_block.get("files", [])
            if not isinstance(history_files, list):
                raise DiscoveryError(
                    f"SEC submissions history list for CIK {identity.cik} is malformed."
                )
            for history_file in history_files:
                preview_annual, preview_quarterly = _member_slots(candidates)
                if (
                    len(set(annual_slots).union(preview_annual)) >= annual_count
                    and len(set(quarterly_slots).union(preview_quarterly)) >= quarterly_count
                ):
                    break
                if not isinstance(history_file, Mapping) or not history_file.get("name"):
                    continue
                history_url = f"{_SUBMISSIONS_ROOT}/{history_file['name']}"
                history = self._get_json(history_url)
                if not isinstance(history, Mapping):
                    raise DiscoveryError(f"SEC history response is malformed: {history_url}")
                candidates.extend(self._parse_requested_columns(company, history))

            member_annual, member_quarterly = _member_slots(candidates)
            for annual_slot, filing in sorted(member_annual.items(), reverse=True):
                if len(annual_slots) >= annual_count:
                    break
                if annual_slot not in annual_slots and filing.accession not in used_accessions:
                    annual_slots[annual_slot] = filing
                    used_accessions.add(filing.accession)
            for quarterly_slot, filing in sorted(member_quarterly.items(), reverse=True):
                if len(quarterly_slots) >= quarterly_count:
                    break
                if (
                    quarterly_slot not in quarterly_slots
                    and filing.accession not in used_accessions
                ):
                    quarterly_slots[quarterly_slot] = filing
                    used_accessions.add(filing.accession)
            if len(annual_slots) >= annual_count and len(quarterly_slots) >= quarterly_count:
                break

        return DiscoveredCompanyWindow(
            annual=tuple(
                sorted(annual_slots.values(), key=lambda filing: filing.report_date, reverse=True)
            ),
            quarterly=tuple(
                sorted(
                    quarterly_slots.values(),
                    key=lambda filing: filing.report_date,
                    reverse=True,
                )
            ),
        )

    def _submissions_payload(self, cik: str) -> Mapping[str, Any]:
        payload = self._get_json(f"{_SUBMISSIONS_ROOT}/CIK{cik}.json")
        if not isinstance(payload, Mapping):
            raise DiscoveryError("SEC submissions response has an unexpected structure.")
        try:
            response_cik = normalize_cik(str(payload["cik"]))
        except (KeyError, TypeError, ValueError, LineageError) as exc:
            raise DiscoveryError(
                f"SEC submissions response for CIK {cik} has no valid CIK."
            ) from exc
        if response_cik != cik:
            raise DiscoveryError(
                f"SEC submissions response CIK {response_cik} does not match {cik}."
            )
        return payload

    def discover_annual_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        selected = self._discover_inline_filings(
            company,
            form="10-K",
            count=count,
            select=_latest_by_fiscal_year,
        )
        return selected[:count]

    def discover_quarterly_inline_filings(self, company: Company, *, count: int) -> list[Filing]:
        selected = self._discover_inline_filings(
            company,
            form="10-Q",
            count=count,
            select=_latest_by_report_date,
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
        payload = self._get_json(submissions_url)
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
                history = self._get_json(history_url)
                if isinstance(history, Mapping):
                    candidates.extend(self._parse_columns(company, history, form=form))
        return select(candidates)

    @staticmethod
    def _parse_columns(
        company: Company, columns: Mapping[str, Any], *, form: str = "10-K"
    ) -> list[Filing]:
        return SecClient._parse_requested_columns(company, columns, forms=frozenset({form}))

    @staticmethod
    def _parse_requested_columns(
        company: Company,
        columns: Mapping[str, Any],
        *,
        forms: frozenset[str] = frozenset({"10-K", "10-Q"}),
    ) -> list[Filing]:
        column_values = _validated_filing_columns(columns, cik=company.cik)
        accessions = column_values["accessionNumber"]
        form_values = column_values["form"]
        inline_values = column_values["isInlineXBRL"]
        filing_dates = column_values["filingDate"]
        report_dates = column_values["reportDate"]
        primary_documents = column_values["primaryDocument"]
        filings: list[Filing] = []
        for index, accession_value in enumerate(accessions):
            filing_form = str(form_values[index])
            inline = inline_values[index]
            if filing_form not in forms or inline not in (1, True, "1"):
                continue
            try:
                accession = str(accession_value).strip()
                filing_date = date.fromisoformat(str(filing_dates[index]))
                report_date = date.fromisoformat(str(report_dates[index]))
                primary_document = str(primary_documents[index]).strip()
                if not accession or not primary_document:
                    raise ValueError("blank accession or primary document")
            except (KeyError, IndexError, TypeError, ValueError) as exc:
                raise DiscoveryError(
                    f"Eligible {filing_form} row {index} for CIK {company.cik} "
                    "has invalid required metadata."
                ) from exc
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
                    registrant_cik=company.cik,
                    archive_owner_cik=company.cik,
                )
            )
        return filings


def _validated_filing_columns(columns: Mapping[str, Any], *, cik: str) -> dict[str, list[Any]]:
    parsed: dict[str, list[Any]] = {}
    lengths: dict[str, int] = {}
    for name in _FILING_COLUMN_NAMES:
        values = columns.get(name)
        if not isinstance(values, list):
            raise DiscoveryError(
                f"SEC submissions filing column {name!r} for CIK {cik} is missing or is not a list."
            )
        parsed[name] = values
        lengths[name] = len(values)

    if len(set(lengths.values())) > 1:
        shape = ", ".join(f"{name}={lengths[name]}" for name in _FILING_COLUMN_NAMES)
        raise DiscoveryError(
            f"SEC submissions filing columns for CIK {cik} have mismatched lengths: {shape}."
        )
    return parsed


def _member_slots(candidates: list[Filing]) -> tuple[dict[int, Filing], dict[date, Filing]]:
    annual: dict[int, Filing] = {}
    quarterly: dict[date, Filing] = {}
    for filing in candidates:
        if filing.form == "10-K":
            annual_slot = filing.report_date.year
            prior = annual.get(annual_slot)
            if prior is None or (filing.filing_date, filing.accession) > (
                prior.filing_date,
                prior.accession,
            ):
                annual[annual_slot] = filing
        else:
            quarterly_slot = filing.report_date
            prior = quarterly.get(quarterly_slot)
            if prior is None or (filing.filing_date, filing.accession) > (
                prior.filing_date,
                prior.accession,
            ):
                quarterly[quarterly_slot] = filing
    return annual, quarterly


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
