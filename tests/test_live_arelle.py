import os
from datetime import date
from pathlib import Path

import pytest

from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.models import Company, Filing

pytestmark = pytest.mark.skipif(
    os.getenv("SEC10K_RUN_LIVE") != "1",
    reason="set SEC10K_RUN_LIVE=1 to contact SEC and load a real filing",
)


def test_arelle_extracts_apples_2025_complete_filing_evidence(tmp_path: Path) -> None:
    company = Company(ticker="AAPL", cik="0000320193", name="Apple Inc.")
    filing = Filing(
        accession="0000320193-25-000079",
        filing_date=date(2025, 10, 31),
        report_date=date(2025, 9, 27),
        form="10-K",
        primary_document="aapl-20250927.htm",
        url=("https://www.sec.gov/Archives/edgar/data/320193/000032019325000079/aapl-20250927.htm"),
    )

    result = ArelleProcessor(
        user_agent=os.environ["SEC_USER_AGENT"],
        cache_directory=tmp_path / "cache" / "arelle",
    ).extract_evidence(company, filing, tmp_path / "capture")

    assert result.fiscal_year == 2025
    assert len(result.observations) > 100
    assert any(
        observation.display_qname == "us-gaap:Assets"
        and observation.typed_value_text == "359241000000"
        for observation in result.observations
    )
    assert len(result.calculation_relationships) > 100
    assert result.source_documents
