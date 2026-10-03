from __future__ import annotations

import json
from datetime import date
from decimal import Decimal, InvalidOperation

from sec_inline_financials.evidence_models import ReportKind
from sec_inline_financials.mapping_models import (
    ConceptCandidate,
    ConceptIdentity,
    DirectMappingRuleSet,
    MappingFactInput,
    MappingSnapshotInput,
    MetricResult,
    MetricWindowEvaluation,
)


def resolve_metric_window(
    *,
    rule_set: DirectMappingRuleSet,
    report_kind: ReportKind,
    snapshots: tuple[MappingSnapshotInput, ...],
    observed_concepts: frozenset[ConceptIdentity],
    candidate_facts: tuple[MappingFactInput, ...],
) -> MetricWindowEvaluation:
    """Resolve one annual or quarterly window without storage or external access."""
    ordered_snapshots = tuple(sorted(snapshots, key=lambda item: item.active_window_rank))
    _validate_inputs(report_kind, ordered_snapshots, candidate_facts)
    observed_keys = {
        (concept.namespace_family, concept.local_name) for concept in observed_concepts
    }
    facts_by_snapshot: dict[int, list[MappingFactInput]] = {
        snapshot.snapshot_id: [] for snapshot in ordered_snapshots
    }
    for fact in candidate_facts:
        facts_by_snapshot[fact.snapshot_id].append(fact)

    results: list[MetricResult] = []
    for rule in rule_set.metrics:
        mapping_seen = any(
            _candidate_key(candidate) in observed_keys for candidate in rule.candidates
        )
        for snapshot in ordered_snapshots:
            selected: tuple[ConceptCandidate, MappingFactInput] | None = None
            trace_candidates: list[dict[str, object]] = []
            for candidate in rule.candidates:
                matching = [
                    fact
                    for fact in facts_by_snapshot[snapshot.snapshot_id]
                    if _fact_matches_candidate(fact, candidate)
                ]
                compatible = [
                    fact
                    for fact in matching
                    if fact.evidence_role == "selected_primary"
                    and not _compatibility_failures(
                        fact,
                        snapshot,
                        rule.expected_period_type,
                        rule.expected_unit_family,
                    )
                ]
                if len(compatible) > 1:
                    raise ValueError(
                        f"Snapshot {snapshot.snapshot_id} has multiple selectable facts for "
                        f"{rule.metric_key} candidate rank {candidate.rank}."
                    )
                if selected is None and compatible:
                    selected = (candidate, compatible[0])
                trace_candidates.append(
                    {
                        "namespace_family": candidate.namespace_family,
                        "local_name": candidate.local_name,
                        "rank": candidate.rank,
                        "observed_in_window": _candidate_key(candidate) in observed_keys,
                        "snapshot_evidence": [
                            {
                                "fact_id": fact.fact_id,
                                "evidence_role": fact.evidence_role,
                                "compatibility_failures": _compatibility_failures(
                                    fact,
                                    snapshot,
                                    rule.expected_period_type,
                                    rule.expected_unit_family,
                                ),
                                "exclusion_reasons": fact.exclusion_reasons,
                                "conflict_reason_codes": fact.conflict_reason_codes,
                            }
                            for fact in matching
                        ],
                    }
                )

            trace = json.dumps(
                {
                    "version": "direct-mapping-trace-v1",
                    "metric_key": rule.metric_key,
                    "snapshot_id": snapshot.snapshot_id,
                    "source_report_evaluation_id": snapshot.report_evaluation_id,
                    "candidates": trace_candidates,
                    "selected_candidate_rank": selected[0].rank if selected else None,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if selected is None:
                results.append(
                    MetricResult(
                        metric_key=rule.metric_key,
                        snapshot_id=snapshot.snapshot_id,
                        report_evaluation_id=snapshot.report_evaluation_id,
                        status="missing",
                        missing_reason=(
                            "no_selectable_fact_for_period" if mapping_seen else "mapping_not_found"
                        ),
                        selected_candidate_rank=None,
                        selected_fact_id=None,
                        typed_value_text=None,
                        resolution_trace_json=trace,
                    )
                )
            else:
                candidate, fact = selected
                results.append(
                    MetricResult(
                        metric_key=rule.metric_key,
                        snapshot_id=snapshot.snapshot_id,
                        report_evaluation_id=snapshot.report_evaluation_id,
                        status="reported",
                        missing_reason=None,
                        selected_candidate_rank=candidate.rank,
                        selected_fact_id=fact.fact_id,
                        typed_value_text=fact.typed_value_text,
                        resolution_trace_json=trace,
                    )
                )

    return MetricWindowEvaluation(
        report_kind=report_kind,
        definition_version=rule_set.definition_version,
        mapping_rule_version=rule_set.mapping_rule_version,
        mapping_rule_hash=rule_set.canonical_hash,
        mapping_rule_json=rule_set.canonical_json,
        source_report_rule_version=ordered_snapshots[0].source_report_rule_version,
        snapshots=ordered_snapshots,
        results=tuple(results),
    )


def _validate_inputs(
    report_kind: ReportKind,
    snapshots: tuple[MappingSnapshotInput, ...],
    facts: tuple[MappingFactInput, ...],
) -> None:
    if not snapshots:
        raise ValueError("A Direct Mapping window must contain at least one snapshot.")
    expected_form = "10-K" if report_kind == "annual" else "10-Q"
    snapshot_ids = {snapshot.snapshot_id for snapshot in snapshots}
    if len(snapshot_ids) != len(snapshots):
        raise ValueError("A Direct Mapping window may not repeat a snapshot.")
    if any(snapshot.form != expected_form for snapshot in snapshots):
        raise ValueError(
            f"A {report_kind} mapping window requires exact-form {expected_form} filings."
        )
    if len({snapshot.active_window_rank for snapshot in snapshots}) != len(snapshots):
        raise ValueError("A Direct Mapping window may not repeat an active rank.")
    if len({snapshot.company_cik for snapshot in snapshots}) != 1:
        raise ValueError("A Direct Mapping window may not mix companies.")
    if len({snapshot.source_report_rule_version for snapshot in snapshots}) != 1:
        raise ValueError("A Direct Mapping window may not mix report rule versions.")
    if any(fact.snapshot_id not in snapshot_ids for fact in facts):
        raise ValueError("Candidate facts must belong to the supplied snapshots.")


def _candidate_key(candidate: ConceptCandidate) -> tuple[str, str]:
    return candidate.namespace_family, candidate.local_name


def _fact_matches_candidate(fact: MappingFactInput, candidate: ConceptCandidate) -> bool:
    return (
        fact.concept.namespace_family == candidate.namespace_family
        and fact.concept.local_name == candidate.local_name
    )


def _compatibility_failures(
    fact: MappingFactInput,
    snapshot: MappingSnapshotInput,
    expected_period_type: str,
    expected_unit_family: str,
) -> tuple[str, ...]:
    failures: list[str] = []
    if fact.concept_period_type != expected_period_type:
        failures.append("concept_period_type_mismatch")
    if fact.context_period_kind != expected_period_type:
        failures.append("context_period_type_mismatch")
    registrant_ciks = snapshot.registrant_ciks
    if not registrant_ciks:
        registrant_ciks = (
            (snapshot.registrant_cik,)
            if snapshot.registrant_cik is not None
            else (snapshot.company_cik,)
        )
    normalized_registrants = {
        normalized for cik in registrant_ciks if (normalized := _normalize_cik(cik)) is not None
    }
    fact_entity = _normalize_cik(fact.entity_identifier)
    if fact_entity is None or fact_entity not in normalized_registrants:
        failures.append("entity_mismatch")
    if fact.unit_family != expected_unit_family:
        failures.append("unsupported_unit")
    if fact.period_end_date != snapshot.report_date:
        failures.append("report_date_mismatch")
    if not _finite_decimal(fact.typed_value_text):
        failures.append("invalid_numeric_text")
    if expected_period_type == "duration" and not _duration_supported(
        fact.period_start_date, fact.period_end_date, snapshot.form
    ):
        failures.append("unsupported_duration")
    return tuple(failures)


def _normalize_cik(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped.isdigit():
        return stripped
    return stripped.lstrip("0") or "0"


def _finite_decimal(value: str | None) -> bool:
    if value is None:
        return False
    try:
        return Decimal(value).is_finite()
    except InvalidOperation:
        return False


def _duration_supported(start: date | None, end: date | None, form: str) -> bool:
    if start is None or end is None:
        return False
    duration = (end - start).days + 1
    if form == "10-K":
        return 300 <= duration <= 400
    return 60 <= duration <= 120
