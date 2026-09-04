from datetime import date
from decimal import Decimal
from pathlib import Path

from sec_inline_financials.models import AnnualResult, Company, Fact, Filing, QuarterlyResult
from sec_inline_financials.service import ReportApplication


def test_application_discovers_processes_and_saves_the_txt_report(tmp_path: Path) -> None:
    company = Company(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = Filing(
        accession="0000320193-25-000079",
        filing_date=date(2025, 10, 31),
        report_date=date(2025, 9, 27),
        form="10-K",
        primary_document="aapl-20250927.htm",
        url="https://www.sec.gov/example.htm",
    )
    fact = Fact(
        concept="us-gaap:Assets",
        label="Assets",
        value=Decimal("359241000000"),
        raw_value="359241000000",
        period_start=None,
        period_end=date(2025, 9, 27),
        unit="iso4217:USD",
        decimals="-6",
        dimensions=(),
        arelle_validity="valid",
        context_id="c1",
    )

    class FakeSecClient:
        def resolve_company(self, ticker: str) -> Company:
            assert ticker == "AAPL"
            return company

        def discover_annual_inline_filings(
            self, requested_company: Company, *, count: int
        ) -> list[Filing]:
            assert requested_company == company
            assert count == 1
            return [filing]

    class FakeProcessor:
        def process(self, requested_filing: Filing) -> AnnualResult:
            assert requested_filing == filing
            return AnnualResult(fiscal_year=2025, filing=filing, primary_facts=(fact,))

    progress: list[str] = []
    application = ReportApplication(
        sec_client=FakeSecClient(), processor=FakeProcessor(), progress_fn=progress.append
    )

    path = application.create_report("AAPL", 1, tmp_path)

    assert path == tmp_path / "AAPL_latest_1_year.txt"
    assert "APPLE INC. (AAPL) — ANNUAL INLINE XBRL DATA" in path.read_text(encoding="utf-8")
    assert progress == [
        "Resolving AAPL and discovering 1 annual Inline XBRL filing...",
        "Processing fiscal year 2025 (0000320193-25-000079) with Arelle [1/1]...",
        "Writing structured report...",
    ]


def test_application_saves_a_separate_latest_twelve_10q_report(tmp_path: Path) -> None:
    company = Company(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    annual_filing = Filing(
        accession="annual-2025",
        filing_date=date(2025, 10, 31),
        report_date=date(2025, 9, 27),
        form="10-K",
        primary_document="annual.htm",
        url="https://www.sec.gov/annual.htm",
    )
    annual_result = AnnualResult(fiscal_year=2025, filing=annual_filing, primary_facts=())
    quarter_ends = (
        (2022, "Q1", date(2022, 3, 31)),
        (2022, "Q2", date(2022, 6, 30)),
        (2022, "Q3", date(2022, 9, 30)),
        (2023, "Q1", date(2023, 3, 31)),
        (2023, "Q2", date(2023, 6, 30)),
        (2023, "Q3", date(2023, 9, 30)),
        (2024, "Q1", date(2024, 3, 31)),
        (2024, "Q2", date(2024, 6, 30)),
        (2024, "Q3", date(2024, 9, 30)),
        (2025, "Q1", date(2025, 3, 31)),
        (2025, "Q2", date(2025, 6, 30)),
        (2025, "Q3", date(2025, 9, 30)),
    )
    quarterly_filings = [
        Filing(
            accession=f"quarter-{fiscal_year}-{fiscal_period}",
            filing_date=report_date,
            report_date=report_date,
            form="10-Q",
            primary_document=f"{fiscal_year}-{fiscal_period}.htm",
            url=f"https://www.sec.gov/{fiscal_year}-{fiscal_period}.htm",
        )
        for fiscal_year, fiscal_period, report_date in reversed(quarter_ends)
    ]
    quarterly_results = {
        filing.accession: QuarterlyResult(
            fiscal_year=fiscal_year,
            fiscal_period=fiscal_period,
            filing=filing,
            primary_facts=(
                Fact(
                    concept="us-gaap:Assets",
                    label="Assets",
                    value=Decimal(str(fiscal_year)),
                    raw_value=str(fiscal_year),
                    period_start=None,
                    period_end=report_date,
                    unit="iso4217:USD",
                    decimals="0",
                    dimensions=(),
                    arelle_validity="valid",
                ),
            ),
        )
        for filing, (fiscal_year, fiscal_period, report_date) in zip(
            reversed(quarterly_filings), quarter_ends, strict=True
        )
    }

    class FakeSecClient:
        def resolve_company(self, ticker: str) -> Company:
            assert ticker == "AAPL"
            return company

        def discover_annual_inline_filings(
            self, requested_company: Company, *, count: int
        ) -> list[Filing]:
            assert requested_company == company
            assert count == 1
            return [annual_filing]

        def discover_quarterly_inline_filings(
            self, requested_company: Company, *, count: int
        ) -> list[Filing]:
            assert requested_company == company
            assert count == 12
            return quarterly_filings

    class FakeProcessor:
        def process(self, requested_filing: Filing) -> AnnualResult:
            assert requested_filing == annual_filing
            return annual_result

        def process_quarterly(self, requested_filing: Filing) -> QuarterlyResult:
            return quarterly_results[requested_filing.accession]

    application = ReportApplication(
        sec_client=FakeSecClient(), processor=FakeProcessor(), progress_fn=lambda _message: None
    )

    annual_path, quarterly_path = application.create_reports(
        "AAPL", years=1, quarters=12, output_dir=tmp_path
    )

    assert annual_path == tmp_path / "AAPL_latest_1_year.txt"
    assert quarterly_path == tmp_path / "AAPL_latest_12_10q_quarters.txt"
    quarterly_text = quarterly_path.read_text(encoding="utf-8")
    assert "QUARTERLY INLINE XBRL DATA" in quarterly_text
    assert "10-Q quarters: FY2022 Q1" in quarterly_text
    assert "FY2025 Q3" in quarterly_text
    assert "Filing: 10-Q" in quarterly_text
