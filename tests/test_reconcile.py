from datetime import date
from decimal import Decimal

from sec_inline_financials.models import Fact
from sec_inline_financials.reconcile import reconcile_primary_facts


def _fact(value: str, *, decimals: str, unit: str = "USD", context_id: str) -> Fact:
    return Fact(
        concept="us-gaap:Assets",
        label="Assets",
        value=Decimal(value),
        raw_value=value,
        period_start=None,
        period_end=date(2025, 9, 27),
        unit=unit,
        decimals=decimals,
        dimensions=(),
        arelle_validity="valid",
        context_id=context_id,
    )


def test_equal_duplicates_collapse_to_the_most_precise_fact() -> None:
    result = reconcile_primary_facts(
        (
            _fact("359241000000", decimals="-6", context_id="c1"),
            _fact("359241000000", decimals="-3", context_id="c2"),
        )
    )

    assert [fact.context_id for fact in result.selected] == ["c2"]
    assert result.selected[0].reconciliation_note == (
        "collapsed 2 exact duplicates; selected context c2 with decimals -3"
    )
    assert result.issues == ()


def test_conflicting_duplicates_are_quarantined_instead_of_guessed_or_averaged() -> None:
    result = reconcile_primary_facts(
        (
            _fact("359241000000", decimals="-6", context_id="c1"),
            _fact("360000000000", decimals="-6", context_id="c2"),
        )
    )

    assert result.selected == ()
    assert len(result.issues) == 1
    assert result.issues[0].concept == "us-gaap:Assets"
    assert result.issues[0].reason == "conflicting dimension-free facts"
    assert result.issues[0].candidate_context_ids == ("c1", "c2")
    assert result.issues[0].candidate_summaries == (
        "c1: value=359241000000; unit=USD; period=instant at 2025-09-27; decimals=-6",
        "c2: value=360000000000; unit=USD; period=instant at 2025-09-27; decimals=-6",
    )
