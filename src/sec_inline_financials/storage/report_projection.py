from __future__ import annotations

from decimal import Decimal

from sec_inline_financials.evidence_models import (
    ContextRecord,
    FilingEvidenceBundle,
    ObservationRecord,
    ReportEvaluation,
    UnitRecord,
)
from sec_inline_financials.models import (
    AnnualResult,
    CalculationRelationship,
    Fact,
    QuarterlyResult,
    ReconciliationIssue,
    ValidationMessage,
)
from sec_inline_financials.storage.evidence_store import EvidenceStore


def project_report(
    bundle: FilingEvidenceBundle, evaluation: ReportEvaluation
) -> AnnualResult | QuarterlyResult:
    """Rebuild the existing detached report model from one explicit snapshot decision set."""
    contexts = {context.key: context for context in bundle.contexts}
    units = {unit.key: unit for unit in bundle.units}
    observations = {observation.key: observation for observation in bundle.observations}
    statuses = {status.fact_key: status for status in evaluation.statuses}

    def to_fact(observation: ObservationRecord) -> Fact:
        context = contexts[observation.context_key or ""]
        unit = units[observation.unit_key or ""]
        status = statuses[observation.key]
        return Fact(
            concept=observation.display_qname or observation.concept_key or "unresolved",
            label=(
                observation.legacy_report_label_text
                or observation.display_qname
                or observation.concept_key
                or "unresolved"
            ),
            value=Decimal(observation.typed_value_text or ""),
            raw_value=observation.raw_value_text or "",
            period_start=context.period_start_date,
            period_end=context.period_end_date or evaluation.report_date,
            unit=unit.legacy_report_unit_text or "",
            decimals=observation.decimals,
            dimensions=context.legacy_report_dimensions,
            arelle_validity=observation.validity_name,
            context_id=context.xml_id or "",
            reconciliation_note=(
                status.selection_note
                or f"selected unique dimension-free {evaluation.report_kind} fact"
            ),
        )

    selected = [
        to_fact(observations[status.fact_key])
        for status in evaluation.statuses
        if status.evidence_role == "selected_primary"
    ]
    selected.sort(key=lambda fact: fact.concept.casefold())
    dimensional_observations = [
        observations[status.fact_key]
        for status in evaluation.statuses
        if status.evidence_role == "dimensional"
    ]
    dimensional_observations.sort(
        key=lambda item: (
            item.top_level_order if item.top_level_order is not None else item.source_order,
            item.source_order,
        )
    )
    dimensional = tuple(to_fact(observation) for observation in dimensional_observations)

    issues: list[ReconciliationIssue] = []
    for issue in evaluation.reconciliation_issues:
        candidates = tuple(observations[key] for key in issue.candidate_fact_keys)
        concept = candidates[0].display_qname if candidates else issue.concept_key
        issues.append(
            ReconciliationIssue(
                concept=concept or issue.concept_key,
                reason=issue.reason_text,
                candidate_context_ids=tuple(
                    contexts[candidate.context_key or ""].xml_id or "" for candidate in candidates
                ),
                candidate_summaries=tuple(
                    _candidate_summary(candidate, contexts, units) for candidate in candidates
                ),
            )
        )
    issues.sort(key=lambda item: item.concept.casefold())
    messages = tuple(
        ValidationMessage(level=item.level, code=item.code, message=item.message_text)
        for item in bundle.validation_messages
    )
    calculation_values = {
        CalculationRelationship(
            role=item.role_uri,
            parent=_display_concept(item.parent_concept_key, bundle),
            child=_display_concept(item.child_concept_key, bundle),
            weight=Decimal(item.exact_weight_text),
        )
        for item in bundle.calculation_relationships
        if item.arcrole_uri == "http://www.xbrl.org/2003/arcrole/summation-item"
    }
    calculations = tuple(
        sorted(
            calculation_values,
            key=lambda item: (item.role, item.parent.casefold(), item.child.casefold()),
        )
    )
    if evaluation.report_kind == "quarterly":
        return QuarterlyResult(
            fiscal_year=bundle.fiscal_year or bundle.filing.report_date.year,
            fiscal_period=bundle.fiscal_period or f"ended-{bundle.filing.report_date.isoformat()}",
            filing=bundle.filing,
            primary_facts=tuple(selected),
            dimensional_facts=dimensional,
            validation_messages=messages,
            calculation_relationships=calculations,
            reconciliation_issues=tuple(issues),
        )
    return AnnualResult(
        fiscal_year=bundle.fiscal_year or bundle.filing.report_date.year,
        filing=bundle.filing,
        primary_facts=tuple(selected),
        dimensional_facts=dimensional,
        validation_messages=messages,
        calculation_relationships=calculations,
        reconciliation_issues=tuple(issues),
    )


def project_stored_report(
    store: EvidenceStore, snapshot_id: int, evaluation_id: int
) -> AnnualResult | QuarterlyResult:
    bundle = store.load_snapshot(snapshot_id)
    evaluation = store.load_report_evaluation(evaluation_id)
    return project_report(bundle, evaluation)


def _candidate_summary(
    observation: ObservationRecord,
    contexts: dict[str, ContextRecord],
    units: dict[str, UnitRecord],
) -> str:
    context = contexts[observation.context_key or ""]
    unit = units[observation.unit_key or ""]
    period_start = context.period_start_date
    period_end = context.period_end_date
    end_text = period_end.isoformat() if period_end is not None else "unknown"
    if period_start is None:
        period = f"instant at {end_text}"
    else:
        period = f"{period_start.isoformat()} to {end_text}"
    return (
        f"{context.xml_id or ''}: value={observation.raw_value_text or ''}; "
        f"unit={unit.legacy_report_unit_text or ''}; period={period}; "
        f"decimals={observation.decimals or 'not reported'}"
    )


def _display_concept(concept_key: str, bundle: FilingEvidenceBundle) -> str:
    for concept in bundle.concepts:
        if concept.key == concept_key:
            return concept.display_qname
    return concept_key
