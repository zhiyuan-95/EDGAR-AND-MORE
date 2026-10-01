from collections.abc import Mapping
from typing import Any

import pytest

from sec_inline_financials.company_lineage import (
    LineageEdge,
    build_company_lineage,
)
from sec_inline_financials.errors import DiscoveryError
from sec_inline_financials.sec_client import SecClient


def _recent(rows: list[dict[str, object]]) -> dict[str, list[object]]:
    keys = (
        "accessionNumber",
        "form",
        "isInlineXBRL",
        "filingDate",
        "reportDate",
        "primaryDocument",
    )
    return {key: [row[key] for row in rows] for key in keys}


def test_company_window_is_current_first_and_reuses_sec_responses() -> None:
    current_url = "https://data.sec.gov/submissions/CIK0000000002.json"
    predecessor_url = "https://data.sec.gov/submissions/CIK0000000001.json"
    payloads: Mapping[str, Any] = {
        current_url: {
            "cik": "2",
            "name": "Current Legal Name",
            "filings": {
                "recent": _recent(
                    [
                        {
                            "accessionNumber": "0000000002-26-000001",
                            "form": "10-K",
                            "isInlineXBRL": 1,
                            "filingDate": "2026-02-01",
                            "reportDate": "2025-12-31",
                            "primaryDocument": "current-annual.htm",
                        },
                        {
                            "accessionNumber": "0000000002-25-000002",
                            "form": "10-Q",
                            "isInlineXBRL": 1,
                            "filingDate": "2025-11-01",
                            "reportDate": "2025-09-30",
                            "primaryDocument": "current-quarter.htm",
                        },
                    ]
                ),
                "files": [],
            },
        },
        predecessor_url: {
            "cik": "1",
            "name": "Predecessor Legal Name",
            "filings": {
                "recent": _recent(
                    [
                        {
                            "accessionNumber": "0000000001-26-000009",
                            "form": "10-K",
                            "isInlineXBRL": 1,
                            "filingDate": "2026-01-15",
                            "reportDate": "2025-12-31",
                            "primaryDocument": "overlap.htm",
                        },
                        {
                            "accessionNumber": "0000000001-25-000001",
                            "form": "10-K",
                            "isInlineXBRL": 1,
                            "filingDate": "2025-02-01",
                            "reportDate": "2024-12-31",
                            "primaryDocument": "old-annual.htm",
                        },
                        {
                            "accessionNumber": "0000000001-25-000002",
                            "form": "10-Q",
                            "isInlineXBRL": 1,
                            "filingDate": "2025-08-01",
                            "reportDate": "2025-06-30",
                            "primaryDocument": "old-quarter.htm",
                        },
                    ]
                ),
                "files": [],
            },
        },
    }
    calls: list[str] = []

    def fetch(url: str, _headers: dict[str, str]) -> Any:
        calls.append(url)
        return payloads[url]

    client = SecClient(user_agent="Example Owner owner@example.com", fetch_json=fetch)
    current = client.resolve_registrant("2")
    predecessor = client.resolve_registrant("1")
    lineage = build_company_lineage(
        company_id=7,
        canonical_current_cik="2",
        members=(current, predecessor),
        edges=(LineageEdge("0000000001", "0000000002"),),
    )

    window = client.discover_company_window(lineage, annual_count=2, quarterly_count=2)

    assert [filing.accession for filing in window.annual] == [
        "0000000002-26-000001",
        "0000000001-25-000001",
    ]
    assert [filing.accession for filing in window.quarterly] == [
        "0000000002-25-000002",
        "0000000001-25-000002",
    ]
    assert window.annual[1].registrant_cik == "0000000001"
    assert calls == [current_url, predecessor_url]


def test_eligible_filing_with_malformed_required_metadata_fails_explicitly() -> None:
    payload = {
        "cik": "2",
        "name": "Current Legal Name",
        "filings": {
            "recent": _recent(
                [
                    {
                        "accessionNumber": "0000000002-26-000001",
                        "form": "10-K",
                        "isInlineXBRL": 1,
                        "filingDate": "not-a-date",
                        "reportDate": "2025-12-31",
                        "primaryDocument": "annual.htm",
                    }
                ]
            ),
            "files": [],
        },
    }
    client = SecClient(
        user_agent="Example Owner owner@example.com",
        fetch_json=lambda _url, _headers: payload,
    )
    identity = client.resolve_registrant("2")
    lineage = build_company_lineage(
        company_id=7,
        canonical_current_cik="2",
        members=(identity,),
        edges=(),
    )

    with pytest.raises(DiscoveryError, match="Eligible 10-K row 0.*0000000002"):
        client.discover_company_window(lineage, annual_count=1, quarterly_count=1)
