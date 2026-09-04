from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from arelle.XmlValidateConst import VALID

from sec_inline_financials import arelle_adapter
from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.models import Filing


class _Concept:
    def __init__(self, label: str) -> None:
        self._label = label

    def label(self, **_kwargs: object) -> str:
        return self._label


def _numeric_fact(
    *,
    concept: str,
    value: str,
    report_date: date,
    period_start: date | None,
    context_id: str,
) -> SimpleNamespace:
    end_datetime = datetime.combine(report_date, datetime.min.time()) + timedelta(days=1)
    context = SimpleNamespace(
        id=context_id,
        endDatetime=end_datetime,
        isInstantPeriod=period_start is None,
        isStartEndPeriod=period_start is not None,
        startDatetime=(
            datetime.combine(period_start, datetime.min.time())
            if period_start is not None
            else None
        ),
        qnameDims={},
    )
    return SimpleNamespace(
        isNumeric=True,
        isNil=False,
        xValid=VALID,
        context=context,
        unit=SimpleNamespace(measures=(("iso4217:USD",), ())),
        xValue=Decimal(value),
        value=value,
        concept=_Concept(concept.rsplit(":", maxsplit=1)[-1]),
        qname=concept,
        decimals="-6",
    )


def test_quarterly_processing_keeps_discrete_quarter_and_instant_facts_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    filing = Filing(
        accession="q-2025-2",
        filing_date=date(2025, 8, 1),
        report_date=date(2025, 6, 30),
        form="10-Q",
        primary_document="q252.htm",
        url="https://www.sec.gov/q252.htm",
    )
    model = SimpleNamespace(
        facts=(
            SimpleNamespace(qname="dei:DocumentFiscalYearFocus", xValue="2025", isNumeric=False),
            SimpleNamespace(qname="dei:DocumentFiscalPeriodFocus", xValue="Q2", isNumeric=False),
            _numeric_fact(
                concept="us-gaap:Assets",
                value="500",
                report_date=filing.report_date,
                period_start=None,
                context_id="instant",
            ),
            _numeric_fact(
                concept="us-gaap:Revenue",
                value="100",
                report_date=filing.report_date,
                period_start=date(2025, 4, 1),
                context_id="quarter",
            ),
            _numeric_fact(
                concept="us-gaap:Revenue",
                value="250",
                report_date=filing.report_date,
                period_start=date(2025, 1, 1),
                context_id="year-to-date",
            ),
        ),
        relationshipSet=lambda _arcrole: SimpleNamespace(modelRelationships=()),
    )

    class FakeSession:
        def __enter__(self) -> "FakeSession":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def run(self, _options: object) -> bool:
            return True

        def get_models(self) -> list[Any]:
            return [model]

        def get_logs(self, _format: str) -> str:
            return "{}"

    monkeypatch.setattr(arelle_adapter, "Session", FakeSession)
    monkeypatch.setattr(arelle_adapter, "ensure_sec_transform_plugin", lambda _cache: tmp_path)

    result = ArelleProcessor(
        user_agent="Example Analyst analyst@example.com", cache_directory=tmp_path / "arelle"
    ).process_quarterly(filing)

    facts = {fact.concept: fact for fact in result.primary_facts}
    assert result.fiscal_year == 2025
    assert result.fiscal_period == "Q2"
    assert facts["us-gaap:Assets"].value == Decimal("500")
    assert facts["us-gaap:Revenue"].value == Decimal("100")
    assert facts["us-gaap:Revenue"].period_start == date(2025, 4, 1)
    assert "quarterly" in facts["us-gaap:Revenue"].reconciliation_note
