from __future__ import annotations

import json
from datetime import date

from sec_inline_financials.direct_mapping import resolve_metric_window
from sec_inline_financials.mapping_models import (
    TARGET_METRIC_KEYS,
    TARGET_METRIC_PERIOD_TYPES,
    ConceptCandidate,
    ConceptIdentity,
    DirectMappingRuleSet,
    MappingFactInput,
    MappingSnapshotInput,
    MetricResult,
    MetricRule,
    MetricWindowEvaluation,
)

_REVENUE_CONCEPT = ConceptIdentity(
    namespace_family="us-gaap",
    namespace_uri="http://fasb.org/us-gaap/2026",
    local_name="Revenue",
    display_qname="us-gaap:Revenue",
)


def test_resolve_metric_window_accepts_fact_entity_from_lineage_registrant_set() -> None:
    evaluation = resolve_metric_window(
        rule_set=_rule_set(),
        report_kind="quarterly",
        snapshots=(
            _snapshot(registrant_cik="0000000002", registrant_ciks=("0000000002", "0000000001")),
        ),
        observed_concepts=frozenset({_REVENUE_CONCEPT}),
        candidate_facts=(_revenue_fact(entity_identifier="0000000001"),),
    )

    result = _metric_result(evaluation, "revenue")
    trace = json.loads(result.resolution_trace_json)
    evidence = trace["candidates"][0]["snapshot_evidence"][0]

    assert result.status == "reported"
    assert result.selected_fact_id == 100
    assert evidence["compatibility_failures"] == []


def test_resolve_metric_window_rejects_predecessor_entity_without_lineage_registrants() -> None:
    evaluation = resolve_metric_window(
        rule_set=_rule_set(),
        report_kind="quarterly",
        snapshots=(_snapshot(registrant_cik="0000000002", registrant_ciks=()),),
        observed_concepts=frozenset({_REVENUE_CONCEPT}),
        candidate_facts=(_revenue_fact(entity_identifier="0000000001"),),
    )

    result = _metric_result(evaluation, "revenue")
    trace = json.loads(result.resolution_trace_json)
    evidence = trace["candidates"][0]["snapshot_evidence"][0]

    assert result.status == "missing"
    assert result.missing_reason == "no_selectable_fact_for_period"
    assert evidence["compatibility_failures"] == ["entity_mismatch"]


def _rule_set() -> DirectMappingRuleSet:
    return DirectMappingRuleSet(
        definition_version="target-metrics-v1",
        mapping_rule_version="test-rules-v1",
        metrics=tuple(
            MetricRule(
                metric_key=metric_key,
                display_name=metric_key.replace("_", " ").title(),
                expected_period_type=TARGET_METRIC_PERIOD_TYPES[metric_key],
                expected_unit_family="monetary",
                candidates=(
                    ConceptCandidate(
                        namespace_family="us-gaap",
                        local_name="Revenue" if metric_key == "revenue" else metric_key,
                        rank=0,
                    ),
                ),
            )
            for metric_key in TARGET_METRIC_KEYS
        ),
    )


def _snapshot(
    *,
    registrant_cik: str | None,
    registrant_ciks: tuple[str, ...],
) -> MappingSnapshotInput:
    return MappingSnapshotInput(
        snapshot_id=10,
        filing_id=20,
        accession="0000000002-26-000001",
        form="10-Q",
        report_date=date(2026, 3, 31),
        active_window_rank=1,
        payload_hash="payload-hash",
        report_evaluation_id=30,
        source_report_rule_version="report-v2",
        company_cik="0000000002",
        registrant_cik=registrant_cik,
        registrant_ciks=registrant_ciks,
    )


def _revenue_fact(*, entity_identifier: str) -> MappingFactInput:
    return MappingFactInput(
        snapshot_id=10,
        fact_id=100,
        concept=_REVENUE_CONCEPT,
        concept_period_type="duration",
        context_period_kind="duration",
        entity_identifier=entity_identifier,
        period_start_date=date(2026, 1, 1),
        period_end_date=date(2026, 3, 31),
        unit_family="monetary",
        typed_value_text="100",
        evidence_role="selected_primary",
    )


def _metric_result(evaluation: MetricWindowEvaluation, metric_key: str) -> MetricResult:
    return next(result for result in evaluation.results if result.metric_key == metric_key)
