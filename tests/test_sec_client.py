from __future__ import annotations

from typing import Any

import pytest

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
