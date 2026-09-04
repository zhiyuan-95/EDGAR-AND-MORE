import os
from datetime import date
from decimal import Decimal

import pytest

from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.models import Filing

pytestmark = pytest.mark.skipif(
    os.getenv("SEC10K_RUN_LIVE") != "1",
    reason="set SEC10K_RUN_LIVE=1 to contact SEC and load a real filing",
)


def test_arelle_extracts_and_reconciles_apples_2025_annual_numeric_facts() -> None:
    filing = Filing(
        accession="0000320193-25-000079",
        filing_date=date(2025, 10, 31),
        report_date=date(2025, 9, 27),
        form="10-K",
        primary_document="aapl-20250927.htm",
        url=("https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"),
    )

    result = ArelleProcessor(user_agent=os.environ["SEC_USER_AGENT"]).process(filing)

    facts = {fact.concept: fact for fact in result.primary_facts}
    assert result.fiscal_year == 2025
    assert len(facts) > 100
    assert facts["us-gaap:Assets"].value == Decimal("359241000000")
    assert facts["us-gaap:Assets"].period_end == date(2025, 9, 27)
    assert facts["us-gaap:Assets"].arelle_validity == "valid"
    assert len(result.calculation_relationships) > 100
