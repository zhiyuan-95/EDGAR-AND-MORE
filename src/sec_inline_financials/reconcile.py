from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, replace

from sec_inline_financials.models import Fact, ReconciliationIssue


@dataclass(frozen=True)
class ReconciliationResult:
    selected: tuple[Fact, ...]
    issues: tuple[ReconciliationIssue, ...]


def _decimals_rank(decimals: str | None) -> float:
    if decimals == "INF":
        return float("inf")
    if decimals is None:
        return float("-inf")
    try:
        return float(int(decimals))
    except ValueError:
        return float("-inf")


def _candidate_summary(fact: Fact) -> str:
    if fact.period_start is None:
        period = f"instant at {fact.period_end.isoformat()}"
    else:
        period = f"{fact.period_start.isoformat()} to {fact.period_end.isoformat()}"
    return (
        f"{fact.context_id}: value={fact.raw_value}; unit={fact.unit}; "
        f"period={period}; decimals={fact.decimals or 'not reported'}"
    )


def reconcile_primary_facts(facts: tuple[Fact, ...]) -> ReconciliationResult:
    """Collapse exact duplicates and quarantine conflicting dimension-free facts."""
    by_concept: dict[str, list[Fact]] = defaultdict(list)
    for fact in facts:
        if fact.dimensions:
            continue
        by_concept[fact.concept].append(fact)

    selected: list[Fact] = []
    issues: list[ReconciliationIssue] = []
    for concept in sorted(by_concept, key=str.casefold):
        candidates = by_concept[concept]
        identities = {
            (fact.value, fact.period_start, fact.period_end, fact.unit) for fact in candidates
        }
        if len(identities) != 1:
            issues.append(
                ReconciliationIssue(
                    concept=concept,
                    reason="conflicting dimension-free facts",
                    candidate_context_ids=tuple(fact.context_id for fact in candidates),
                    candidate_summaries=tuple(_candidate_summary(fact) for fact in candidates),
                )
            )
            continue
        chosen = sorted(
            candidates,
            key=lambda fact: (-_decimals_rank(fact.decimals), fact.context_id),
        )[0]
        if len(candidates) > 1:
            chosen = replace(
                chosen,
                reconciliation_note=(
                    f"collapsed {len(candidates)} exact duplicates; selected context "
                    f"{chosen.context_id} with decimals {chosen.decimals or 'not reported'}"
                ),
            )
        selected.append(chosen)

    return ReconciliationResult(selected=tuple(selected), issues=tuple(issues))
