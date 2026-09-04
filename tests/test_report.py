from datetime import date
from decimal import Decimal

from sec_inline_financials.models import (
    AnnualResult,
    CalculationRelationship,
    Company,
    Fact,
    Filing,
    ReconciliationIssue,
    ReportData,
    ValidationMessage,
)
from sec_inline_financials.report import render_report


def _filing(year: int, accession: str) -> Filing:
    return Filing(
        accession=accession,
        filing_date=date(year, 11, 1),
        report_date=date(year, 9, 28),
        form="10-K",
        primary_document=f"aapl-{year}.htm",
        url=f"https://www.sec.gov/Archives/edgar/data/320193/{accession.replace('-', '')}/aapl.htm",
    )


def _fact(concept: str, label: str, value: str, year: int, unit: str = "USD") -> Fact:
    return Fact(
        concept=concept,
        label=label,
        value=Decimal(value),
        raw_value=value,
        period_start=None,
        period_end=date(year, 9, 28),
        unit=unit,
        decimals="-6",
        dimensions=(),
        arelle_validity="valid",
    )


def test_report_is_a_concept_union_table_followed_by_cell_evidence() -> None:
    report = ReportData(
        company=Company(ticker="AAPL", cik="0000320193", name="Apple Inc."),
        annual_results=(
            AnnualResult(
                fiscal_year=2024,
                filing=_filing(2024, "0000320193-24-000123"),
                primary_facts=(
                    _fact("us-gaap:Assets", "Assets", "364980000000", 2024),
                    _fact("us-gaap:Liabilities", "Liabilities", "308030000000", 2024),
                ),
            ),
            AnnualResult(
                fiscal_year=2025,
                filing=_filing(2025, "0000320193-25-000079"),
                primary_facts=(_fact("us-gaap:Assets", "Assets", "359241000000", 2025),),
            ),
        ),
    )

    text = render_report(report)

    assert "APPLE INC. (AAPL) — ANNUAL INLINE XBRL DATA" in text
    header = next(line for line in text.splitlines() if line.startswith("CONCEPT"))
    assert "CONCEPT / LABEL" not in header
    assert "2024" in text and "2025" in text
    assets_row = next(
        line for line in text.splitlines() if line.startswith("us-gaap:Assets") and " | " in line
    )
    liabilities_row = next(
        line
        for line in text.splitlines()
        if line.startswith("us-gaap:Liabilities") and " | " in line
    )
    assert "364,980,000,000 [E001]" in assets_row
    assert "359,241,000,000 [E002]" in assets_row
    assert "308,030,000,000 [E003]" in liabilities_row
    assert "—" in liabilities_row
    assert text.index("EVIDENCE") > text.index(assets_row)
    assert "[E001] us-gaap:Assets — Assets" in text
    assert "Period: instant at 2024-09-28" in text
    assert "Arelle validity: valid" in text
    assert "Accession: 0000320193-24-000123" in text


def test_report_preserves_dimensional_validation_calculation_and_conflict_evidence() -> None:
    filing = _filing(2025, "0000320193-25-000079")
    dimensional = Fact(
        concept="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
        label="Net sales",
        value=Decimal("100"),
        raw_value="100",
        period_start=date(2024, 9, 29),
        period_end=date(2025, 9, 27),
        unit="iso4217:USD",
        decimals="-6",
        dimensions=("srt:ProductOrServiceAxis=aapl:IPhoneMember",),
        arelle_validity="valid",
        context_id="c-product",
    )
    report = ReportData(
        company=Company(ticker="AAPL", cik="0000320193", name="Apple Inc."),
        annual_results=(
            AnnualResult(
                fiscal_year=2025,
                filing=filing,
                primary_facts=(),
                dimensional_facts=(dimensional,),
                validation_messages=(
                    ValidationMessage(
                        level="warning", code="calc:inconsistency", message="Calculation differs"
                    ),
                ),
                calculation_relationships=(
                    CalculationRelationship(
                        role="Income Statement",
                        parent="us-gaap:Revenue",
                        child="us-gaap:ProductRevenue",
                        weight=Decimal("1"),
                    ),
                ),
                reconciliation_issues=(
                    ReconciliationIssue(
                        concept="us-gaap:Assets",
                        reason="conflicting dimension-free facts",
                        candidate_context_ids=("c1", "c2"),
                        candidate_summaries=(
                            "c1: value=100; unit=USD",
                            "c2: value=200; unit=USD",
                        ),
                    ),
                ),
            ),
        ),
    )

    text = render_report(report)

    revenue_row = next(
        line
        for line in text.splitlines()
        if line.startswith("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax")
        and " | " in line
    )
    assets_row = next(
        line for line in text.splitlines() if line.startswith("us-gaap:Assets") and " | " in line
    )
    assert "DIMENSIONAL [D001]" in revenue_row
    assert "CONFLICT [R001]" in assets_row
    assert "[D001] us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" in text
    assert "srt:ProductOrServiceAxis=aapl:IPhoneMember" in text
    assert "[R001] us-gaap:Assets" in text
    assert "Candidate contexts: c1, c2" in text
    assert "Candidate: c1: value=100; unit=USD" in text
    assert "Candidate: c2: value=200; unit=USD" in text
    assert "ARELLE VALIDATION MESSAGES" in text
    assert "[warning] calc:inconsistency: Calculation differs" in text
    assert "CALCULATION RELATIONSHIPS" in text
    assert "us-gaap:Revenue = (1) us-gaap:ProductRevenue" in text
