import re
from datetime import date
from decimal import Decimal

from sec_inline_financials.models import AnnualResult, Company, Fact, Filing, ReportData
from sec_inline_financials.report import render_report


def _fact(
    concept: str,
    raw_value: str,
    *,
    dimensions: tuple[str, ...] = (),
) -> Fact:
    return Fact(
        concept=concept,
        label=concept,
        value=Decimal(raw_value),
        raw_value=raw_value,
        period_start=date(2025, 1, 1),
        period_end=date(2025, 12, 31),
        unit="iso4217:USD",
        decimals="-6",
        dimensions=dimensions,
        arelle_validity="VALID",
        context_id=f"context-{concept}-{raw_value}",
    )


def test_render_report_nests_actual_dimensional_values_without_summing() -> None:
    filing = Filing(
        accession="0000000000-26-000001",
        filing_date=date(2026, 2, 1),
        report_date=date(2025, 12, 31),
        form="10-K",
        primary_document="example.htm",
        url="https://www.sec.gov/example.htm",
        registrant_cik="0000000000",
        archive_owner_cik="0000000000",
    )
    consolidated_concept = "example:ConsolidatedAndDimensional"
    dimensional_only_concept = "example:DimensionalOnly"
    rendered = render_report(
        ReportData(
            company=Company(ticker="EX", cik="0000000000", name="Example Corp"),
            annual_results=(
                AnnualResult(
                    fiscal_year=2025,
                    filing=filing,
                    primary_facts=(_fact(consolidated_concept, "100"),),
                    dimensional_facts=(
                        _fact(
                            consolidated_concept,
                            "60",
                            dimensions=("ProductAxis=ProductAMember",),
                        ),
                        _fact(
                            consolidated_concept,
                            "30",
                            dimensions=("ProductAxis=ProductBMember",),
                        ),
                        _fact(
                            dimensional_only_concept,
                            "15",
                            dimensions=("GeographyAxis=CanadaMember",),
                        ),
                        _fact(
                            dimensional_only_concept,
                            "5",
                            dimensions=("GeographyAxis=MexicoMember",),
                        ),
                    ),
                ),
            ),
        )
    )

    table = rendered.split("\n\nEVIDENCE", maxsplit=1)[0]

    assert re.search(rf"^{consolidated_concept}\s+\|\s+100 \[E001\]$", table, re.MULTILINE)
    assert re.search(r"^  ProductAxis=ProductAMember\s+\|\s+60 \[D001\.1\]$", table, re.MULTILINE)
    assert re.search(r"^  ProductAxis=ProductBMember\s+\|\s+30 \[D001\.2\]$", table, re.MULTILINE)
    assert re.search(
        rf"^{dimensional_only_concept}\s+\|\s+NO CONSOLIDATED FACT \[D002\]$",
        table,
        re.MULTILINE,
    )
    assert re.search(r"^  GeographyAxis=CanadaMember\s+\|\s+15 \[D002\.1\]$", table, re.MULTILINE)
    assert re.search(r"^  GeographyAxis=MexicoMember\s+\|\s+5 \[D002\.2\]$", table, re.MULTILINE)
    assert "90 [" not in table
    assert "20 [" not in table
