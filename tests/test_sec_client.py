from typing import Any

import pytest

from sec_inline_financials.errors import DiscoveryError
from sec_inline_financials.sec_client import SecClient


def test_requires_an_identifying_sec_user_agent_with_contact_email() -> None:
    with pytest.raises(DiscoveryError, match="name or organization and a contact email"):
        SecClient(user_agent="anonymous")


def test_discovers_latest_inline_10k_per_fiscal_year_across_submission_files() -> None:
    payloads: dict[str, Any] = {
        "https://www.sec.gov/files/company_tickers.json": {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
        },
        "https://data.sec.gov/submissions/CIK0000320193.json": {
            "filings": {
                "recent": {
                    "accessionNumber": [
                        "0000320193-25-000079",
                        "0000320193-25-000080",
                        "0000320193-24-000123",
                        "0000320193-23-000106",
                    ],
                    "filingDate": ["2025-10-31", "2025-11-03", "2024-11-01", "2023-11-03"],
                    "reportDate": ["2025-09-27", "2025-09-27", "2024-09-28", "2023-09-30"],
                    "form": ["10-K", "10-K/A", "10-K", "10-K"],
                    "primaryDocument": ["aapl25.htm", "aapl25a.htm", "aapl24.htm", "aapl23.htm"],
                    "isInlineXBRL": [1, 1, 1, 0],
                },
                "files": [{"name": "CIK0000320193-submissions-001.json", "filingCount": 1}],
            }
        },
        "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json": {
            "accessionNumber": ["0000320193-22-000108"],
            "filingDate": ["2022-10-28"],
            "reportDate": ["2022-09-24"],
            "form": ["10-K"],
            "primaryDocument": ["aapl22.htm"],
            "isInlineXBRL": [1],
        },
    }
    requested_urls: list[str] = []

    def fetch_json(url: str, _headers: dict[str, str]) -> Any:
        requested_urls.append(url)
        return payloads[url]

    client = SecClient(user_agent="Example Analyst analyst@example.com", fetch_json=fetch_json)

    company = client.resolve_company("aapl")
    filings = client.discover_annual_inline_filings(company, count=3)

    assert company.ticker == "AAPL"
    assert company.cik == "0000320193"
    assert [filing.report_date.year for filing in filings] == [2025, 2024, 2022]
    assert all(filing.form == "10-K" for filing in filings)
    assert filings[0].url.endswith("/320193/000032019325000079/aapl25.htm")
    assert requested_urls[-1].endswith("CIK0000320193-submissions-001.json")


def test_reports_when_requested_inline_history_is_not_available() -> None:
    def fetch_json(url: str, _headers: dict[str, str]) -> Any:
        if url.endswith("company_tickers.json"):
            return {"0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}}
        return {
            "filings": {
                "recent": {
                    "accessionNumber": ["0000320193-25-000079"],
                    "filingDate": ["2025-10-31"],
                    "reportDate": ["2025-09-27"],
                    "form": ["10-K"],
                    "primaryDocument": ["aapl25.htm"],
                    "isInlineXBRL": [1],
                },
                "files": [],
            }
        }

    client = SecClient(user_agent="Example Analyst analyst@example.com", fetch_json=fetch_json)
    company = client.resolve_company("AAPL")

    with pytest.raises(
        DiscoveryError,
        match="Requested 5 annual Inline XBRL 10-K fiscal years, but SEC history has 1",
    ):
        client.discover_annual_inline_filings(company, count=5)


def test_discovers_latest_twelve_inline_10q_filings_across_submission_files() -> None:
    def columns(rows: list[tuple[str, str, str, str, str, int]]) -> dict[str, list[object]]:
        return {
            "accessionNumber": [row[0] for row in rows],
            "filingDate": [row[1] for row in rows],
            "reportDate": [row[2] for row in rows],
            "form": [row[3] for row in rows],
            "primaryDocument": [row[4] for row in rows],
            "isInlineXBRL": [row[5] for row in rows],
        }

    recent_rows = [
        ("q-2025-3", "2025-11-01", "2025-09-30", "10-Q", "q253.htm", 1),
        ("q-2025-3a", "2025-11-02", "2025-09-30", "10-Q/A", "q253a.htm", 1),
        ("q-2025-2", "2025-08-01", "2025-06-30", "10-Q", "q252.htm", 1),
        ("q-2025-1", "2025-05-01", "2025-03-31", "10-Q", "q251.htm", 1),
        ("k-2024", "2025-02-01", "2024-12-31", "10-K", "k24.htm", 1),
        ("q-2024-3", "2024-11-01", "2024-09-30", "10-Q", "q243.htm", 1),
        ("q-2024-2", "2024-08-01", "2024-06-30", "10-Q", "q242.htm", 1),
        ("q-2024-1", "2024-05-01", "2024-03-31", "10-Q", "q241.htm", 1),
    ]
    history_rows = [
        ("q-2023-3", "2023-11-01", "2023-09-30", "10-Q", "q233.htm", 1),
        ("q-2023-2", "2023-08-01", "2023-06-30", "10-Q", "q232.htm", 1),
        ("q-2023-1", "2023-05-01", "2023-03-31", "10-Q", "q231.htm", 1),
        ("q-2022-3", "2022-11-01", "2022-09-30", "10-Q", "q223.htm", 1),
        ("q-2022-2", "2022-08-01", "2022-06-30", "10-Q", "q222.htm", 1),
        ("q-2022-1", "2022-05-01", "2022-03-31", "10-Q", "q221.htm", 1),
        ("q-2021-3", "2021-11-01", "2021-09-30", "10-Q", "q213.htm", 0),
    ]
    payloads: dict[str, Any] = {
        "https://www.sec.gov/files/company_tickers.json": {
            "0": {"cik_str": 320193, "ticker": "AAPL", "title": "Apple Inc."}
        },
        "https://data.sec.gov/submissions/CIK0000320193.json": {
            "filings": {
                "recent": columns(recent_rows),
                "files": [{"name": "CIK0000320193-submissions-001.json"}],
            }
        },
        "https://data.sec.gov/submissions/CIK0000320193-submissions-001.json": columns(
            history_rows
        ),
    }

    client = SecClient(
        user_agent="Example Analyst analyst@example.com",
        fetch_json=lambda url, _headers: payloads[url],
    )
    company = client.resolve_company("AAPL")

    filings = client.discover_quarterly_inline_filings(company, count=12)

    assert len(filings) == 12
    assert all(filing.form == "10-Q" for filing in filings)
    assert filings[0].accession == "q-2025-3"
    assert filings[-1].accession == "q-2022-1"
    assert filings[0].report_date > filings[-1].report_date
