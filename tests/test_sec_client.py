from __future__ import annotations

from typing import Any

import pytest

from sec_inline_financials.company_lineage import (
    LineageEdge,
    RegistrantIdentity,
    build_company_lineage,
)
from sec_inline_financials.errors import DiscoveryError
from sec_inline_financials.models import Company
from sec_inline_financials.sec_client import SecClient

_COMPANY = Company(ticker="TST", cik="0000123456", name="Test Company")
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK0000123456.json"


def _recent_columns(**overrides: list[Any]) -> dict[str, list[Any]]:
    columns: dict[str, list[Any]] = {
        "accessionNumber": ["0000123456-26-000001"],
        "form": ["10-K"],
        "isInlineXBRL": [1],
        "filingDate": ["2026-02-01"],
        "reportDate": ["2025-12-31"],
        "primaryDocument": ["test-20251231.htm"],
    }
    columns.update(overrides)
    return columns


def _client_for_recent(recent: dict[str, Any]) -> SecClient:
    def fetch_json(url: str, _headers: dict[str, str]) -> Any:
        if url == _SUBMISSIONS_URL:
            return {"filings": {"recent": recent, "files": []}}
        raise AssertionError(f"Unexpected SEC URL: {url}")

    return SecClient(user_agent="SEC Client Test test@example.com", fetch_json=fetch_json)


def test_discover_annual_inline_filings_parses_valid_recent_row() -> None:
    client = _client_for_recent(_recent_columns())

    filings = client.discover_annual_inline_filings(_COMPANY, count=1)

    assert len(filings) == 1
    assert filings[0].accession == "0000123456-26-000001"
    assert filings[0].primary_document == "test-20251231.htm"


def test_discover_annual_inline_filings_rejects_missing_accession_column() -> None:
    recent = _recent_columns()
    del recent["accessionNumber"]
    client = _client_for_recent(recent)

    with pytest.raises(DiscoveryError, match="accessionNumber"):
        client.discover_annual_inline_filings(_COMPANY, count=1)


def test_discover_annual_inline_filings_rejects_mismatched_column_lengths() -> None:
    recent = _recent_columns(form=[])
    client = _client_for_recent(recent)

    with pytest.raises(DiscoveryError, match="mismatched lengths"):
        client.discover_annual_inline_filings(_COMPANY, count=1)


def test_resolve_registrant_returns_verified_sec_identity() -> None:
    client = SecClient(
        user_agent="SEC Client Test test@example.com",
        fetch_json=lambda *_: {"cik": 123456, "name": "  Test Company, Inc.  "},
    )

    assert client.resolve_registrant("123456") == RegistrantIdentity(
        cik="0000123456",
        legal_name="Test Company, Inc.",
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    (
        ([], "unexpected structure"),
        ({"name": "Missing CIK"}, "has no valid CIK"),
        ({"cik": 999999, "name": "Wrong CIK"}, "does not match"),
        ({"cik": "١٢٣٤٥٦", "name": "Unicode CIK"}, "has no valid CIK"),
        ({"cik": 123456, "name": "  "}, "has no name"),
    ),
)
def test_resolve_registrant_rejects_malformed_identity_payloads(
    payload: object,
    message: str,
) -> None:
    client = SecClient(
        user_agent="SEC Client Test test@example.com",
        fetch_json=lambda *_: payload,
    )

    with pytest.raises(DiscoveryError, match=message):
        client.resolve_registrant("123456")


def test_discover_company_window_fills_lineage_gaps_without_overwriting_current_slots() -> None:
    successor_cik = "0000000002"
    predecessor_cik = "0000000001"
    lineage = build_company_lineage(
        company_id=1,
        canonical_current_cik=successor_cik,
        members=(
            RegistrantIdentity(successor_cik, "Successor Corp"),
            RegistrantIdentity(predecessor_cik, "Predecessor Corp"),
        ),
        edges=(LineageEdge(predecessor_cik, successor_cik),),
    )
    payloads = {
        successor_cik: _submissions_payload(
            successor_cik,
            (
                _filing_row(
                    accession="0000000002-26-000010",
                    form="10-K",
                    filing_date="2026-02-15",
                    report_date="2025-12-31",
                    primary_document="successor-20251231.htm",
                ),
                _filing_row(
                    accession="0000000002-26-000020",
                    form="10-Q",
                    filing_date="2026-05-05",
                    report_date="2026-03-31",
                    primary_document="successor-20260331.htm",
                ),
            ),
        ),
        predecessor_cik: _submissions_payload(
            predecessor_cik,
            (
                _filing_row(
                    accession="0000000001-26-000010",
                    form="10-K",
                    filing_date="2026-03-01",
                    report_date="2025-12-31",
                    primary_document="predecessor-20251231.htm",
                ),
                _filing_row(
                    accession="0000000001-25-000010",
                    form="10-K",
                    filing_date="2025-02-15",
                    report_date="2024-12-31",
                    primary_document="predecessor-20241231.htm",
                ),
                _filing_row(
                    accession="0000000001-26-000020",
                    form="10-Q",
                    filing_date="2026-05-20",
                    report_date="2026-03-31",
                    primary_document="predecessor-20260331.htm",
                ),
                _filing_row(
                    accession="0000000001-26-000021",
                    form="10-Q",
                    filing_date="2026-02-01",
                    report_date="2025-12-31",
                    primary_document="predecessor-20251231q.htm",
                ),
            ),
        ),
    }

    def fetch_json(url: str, _headers: dict[str, str]) -> Any:
        for cik, payload in payloads.items():
            if url.endswith(f"/CIK{cik}.json"):
                return payload
        raise AssertionError(f"Unexpected SEC URL: {url}")

    client = SecClient(user_agent="SEC Client Test test@example.com", fetch_json=fetch_json)

    window = client.discover_company_window(lineage, annual_count=2, quarterly_count=2)

    assert tuple(filing.accession for filing in window.annual) == (
        "0000000002-26-000010",
        "0000000001-25-000010",
    )
    assert tuple(filing.registrant_cik for filing in window.annual) == (
        successor_cik,
        predecessor_cik,
    )
    assert tuple(filing.accession for filing in window.quarterly) == (
        "0000000002-26-000020",
        "0000000001-26-000021",
    )


def test_discover_company_window_loads_history_until_gaps_are_filled_and_deduplicates() -> None:
    cik = _COMPANY.cik
    recent = _filing_row(
        accession="0000123456-26-000001",
        form="10-K",
        filing_date="2026-02-01",
        report_date="2025-12-31",
        primary_document="current.htm",
    )
    prior = _filing_row(
        accession="0000123456-25-000001",
        form="10-K",
        filing_date="2025-02-01",
        report_date="2024-12-31",
        primary_document="prior.htm",
    )
    quarter = _filing_row(
        accession="0000123456-26-000002",
        form="10-Q",
        filing_date="2026-05-01",
        report_date="2026-03-31",
        primary_document="quarter.htm",
    )
    main = _submissions_payload(cik, (recent,))
    main["name"] = _COMPANY.name
    main["filings"]["files"] = [{"name": "old-one.json"}, {"name": "unused.json"}]
    history = _submissions_payload(cik, (recent, prior, quarter))["filings"]["recent"]
    requested: list[str] = []

    def fetch_json(url: str, _headers: dict[str, str]) -> Any:
        requested.append(url)
        if url.endswith(f"/CIK{cik}.json"):
            return main
        if url.endswith("/old-one.json"):
            return history
        raise AssertionError(f"History traversal should have stopped before {url}")

    lineage = build_company_lineage(
        company_id=1,
        canonical_current_cik=cik,
        members=(RegistrantIdentity(cik, _COMPANY.name),),
        edges=(),
    )

    window = SecClient(
        user_agent="SEC Client Test test@example.com",
        fetch_json=fetch_json,
    ).discover_company_window(lineage, annual_count=2, quarterly_count=1)

    assert tuple(filing.accession for filing in window.annual) == (
        recent["accessionNumber"],
        prior["accessionNumber"],
    )
    assert tuple(filing.accession for filing in window.quarterly) == (quarter["accessionNumber"],)
    assert requested[-1].endswith("/old-one.json")


def test_discover_company_window_rejects_malformed_history_file_list() -> None:
    payload = _submissions_payload(_COMPANY.cik, ())
    payload["filings"]["files"] = "not-a-list"
    lineage = build_company_lineage(
        company_id=1,
        canonical_current_cik=_COMPANY.cik,
        members=(RegistrantIdentity(_COMPANY.cik, _COMPANY.name),),
        edges=(),
    )
    client = SecClient(
        user_agent="SEC Client Test test@example.com",
        fetch_json=lambda *_: payload,
    )

    with pytest.raises(DiscoveryError, match="history list.*malformed"):
        client.discover_company_window(lineage, annual_count=1, quarterly_count=1)


def _filing_row(
    *,
    accession: str,
    form: str,
    filing_date: str,
    report_date: str,
    primary_document: str,
) -> dict[str, Any]:
    return {
        "accessionNumber": accession,
        "form": form,
        "isInlineXBRL": 1,
        "filingDate": filing_date,
        "reportDate": report_date,
        "primaryDocument": primary_document,
    }


def _submissions_payload(cik: str, rows: tuple[dict[str, Any], ...]) -> dict[str, Any]:
    return {
        "cik": int(cik),
        "filings": {
            "recent": {
                column: [row[column] for row in rows]
                for column in (
                    "accessionNumber",
                    "form",
                    "isInlineXBRL",
                    "filingDate",
                    "reportDate",
                    "primaryDocument",
                )
            },
            "files": [],
        },
    }
