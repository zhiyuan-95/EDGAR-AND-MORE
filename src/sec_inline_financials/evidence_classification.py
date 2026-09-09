from __future__ import annotations

from collections import defaultdict
from decimal import Decimal, InvalidOperation

from arelle.XmlValidateConst import VALID

from sec_inline_financials.evidence_models import (
    FactReportStatus,
    FilingEvidenceBundle,
    ObservationRecord,
    ReconciliationIssueRecord,
    ReportEvaluation,
    ReportKind,
)

REPORT_RULE_VERSION = "report-v1"


def _decimals_rank(decimals: str | None) -> float:
    if decimals == "INF":
        return float("inf")
    if decimals is None:
        return float("-inf")
    try:
        return float(int(decimals))
    except ValueError:
        return float("-inf")


def _reason(code: str, detail: str) -> tuple[str, str]:
    return code, detail


def _eligibility_reasons(
    observation: ObservationRecord,
    bundle: FilingEvidenceBundle,
    report_kind: ReportKind,
) -> tuple[tuple[str, str], ...]:
    contexts = {context.key: context for context in bundle.contexts}
    units = {unit.key: unit for unit in bundle.units}
    reasons: list[tuple[str, str]] = []
    if observation.top_level_order is None:
        reasons.append(
            _reason(
                "LEGACY_REPORT_TOP_LEVEL_ONLY",
                "report-v1 includes only the existing top-level Arelle fact traversal",
            )
        )
    if observation.concept_key is None:
        reasons.append(_reason("UNRESOLVED_CONCEPT", "concept could not be resolved"))
    if observation.is_numeric is not True:
        reasons.append(_reason("NON_NUMERIC", "observation is not numeric"))
    if observation.is_nil:
        reasons.append(_reason("NIL_VALUE", "observation explicitly reports nil"))
    if observation.validity_code is None or observation.validity_code < VALID:
        reasons.append(
            _reason(
                "ARELLE_INVALID",
                f"Arelle validity is {observation.validity_name}",
            )
        )
    if observation.numeric_conversion_error is not None:
        reasons.append(_reason("NUMERIC_CONVERSION_FAILED", observation.numeric_conversion_error))
    elif observation.is_numeric and not observation.is_nil:
        try:
            number = Decimal(observation.typed_value_text or "")
            if not number.is_finite():
                reasons.append(_reason("NON_FINITE_NUMERIC", "numeric value is not finite"))
        except (InvalidOperation, ValueError):
            reasons.append(
                _reason("NUMERIC_CONVERSION_FAILED", "typed numeric value is not a decimal")
            )

    context = contexts.get(observation.context_key or "")
    if context is None:
        reasons.append(_reason("MISSING_CONTEXT", "contextRef did not resolve uniquely"))
    else:
        if context.period_kind not in {"instant", "duration"}:
            reasons.append(
                _reason(
                    "UNSUPPORTED_PERIOD_TYPE",
                    f"context period type is {context.period_kind}",
                )
            )
        elif context.period_end_date is None:
            reasons.append(_reason("UNSUPPORTED_PERIOD_TYPE", "context period end is incomplete"))
        if (
            context.period_end_date is not None
            and context.period_end_date != bundle.filing.report_date
        ):
            reasons.append(
                _reason(
                    "PERIOD_END_MISMATCH",
                    f"context ends {context.period_end_date.isoformat()}, not report date "
                    f"{bundle.filing.report_date.isoformat()}",
                )
            )
        if context.period_kind == "duration":
            if context.period_start_date is None or context.period_end_date is None:
                reasons.append(
                    _reason("UNSUPPORTED_PERIOD_TYPE", "duration context dates are incomplete")
                )
            else:
                duration_days = (context.period_end_date - context.period_start_date).days + 1
                minimum, maximum = (300, 400) if report_kind == "annual" else (60, 120)
                if not minimum <= duration_days <= maximum:
                    code = (
                        "ANNUAL_DURATION_OUT_OF_RANGE"
                        if report_kind == "annual"
                        else "QUARTER_DURATION_OUT_OF_RANGE"
                    )
                    reasons.append(
                        _reason(
                            code,
                            f"duration is {duration_days} days; expected {minimum}-{maximum}",
                        )
                    )
    if observation.is_numeric and observation.unit_key not in units:
        reasons.append(_reason("MISSING_UNIT", "unitRef did not resolve uniquely"))
    return tuple(reasons)


def classify_report(
    bundle: FilingEvidenceBundle,
    report_kind: ReportKind,
    rule_version: str = REPORT_RULE_VERSION,
) -> ReportEvaluation:
    """Apply the legacy report rules without removing any observed filing fact."""
    contexts = {context.key: context for context in bundle.contexts}
    units = {unit.key: unit for unit in bundle.units}
    statuses: dict[str, FactReportStatus] = {}
    primary_by_concept: dict[str, list[ObservationRecord]] = defaultdict(list)

    for observation in sorted(bundle.observations, key=lambda item: item.source_order):
        reasons = _eligibility_reasons(observation, bundle, report_kind)
        if reasons:
            statuses[observation.key] = FactReportStatus(
                fact_key=observation.key,
                evidence_role="excluded",
                exclusion_reasons=reasons,
            )
            continue
        context = contexts[observation.context_key or ""]
        if context.dimensions:
            statuses[observation.key] = FactReportStatus(
                fact_key=observation.key,
                evidence_role="dimensional",
            )
            continue
        primary_by_concept[observation.concept_key or ""].append(observation)

    issues: list[ReconciliationIssueRecord] = []
    for concept_key in sorted(primary_by_concept):
        candidates = primary_by_concept[concept_key]
        identities: set[tuple[Decimal, object, object, str]] = set()
        for observation in candidates:
            context = contexts[observation.context_key or ""]
            unit = units[observation.unit_key or ""]
            identities.add(
                (
                    Decimal(observation.typed_value_text or ""),
                    context.period_start_date,
                    context.period_end_date,
                    unit.legacy_report_unit_text or "",
                )
            )
        if len(identities) != 1:
            issue_order = len(issues)
            for observation in candidates:
                statuses[observation.key] = FactReportStatus(
                    fact_key=observation.key,
                    evidence_role="conflict_candidate",
                    exclusion_reasons=(
                        _reason(
                            "CONFLICTING_DIMENSION_FREE_FACT",
                            "dimension-free candidates disagree on value, period, or unit",
                        ),
                    ),
                )
            issues.append(
                ReconciliationIssueRecord(
                    concept_key=concept_key,
                    reason_code="CONFLICTING_DIMENSION_FREE_FACT",
                    reason_text="conflicting dimension-free facts",
                    issue_order=issue_order,
                    candidate_fact_keys=tuple(observation.key for observation in candidates),
                )
            )
            continue

        chosen = sorted(
            candidates,
            key=lambda item: (
                -_decimals_rank(item.decimals),
                contexts[item.context_key or ""].xml_id or "",
                item.source_order,
            ),
        )[0]
        context_id = contexts[chosen.context_key or ""].xml_id or ""
        if len(candidates) == 1:
            note = f"selected unique dimension-free {report_kind} fact"
        else:
            note = (
                f"collapsed {len(candidates)} exact duplicates; selected context "
                f"{context_id} with decimals {chosen.decimals or 'not reported'}"
            )
        statuses[chosen.key] = FactReportStatus(
            fact_key=chosen.key,
            evidence_role="selected_primary",
            selection_note=note,
        )
        for observation in candidates:
            if observation.key == chosen.key:
                continue
            statuses[observation.key] = FactReportStatus(
                fact_key=observation.key,
                evidence_role="duplicate_not_selected",
                exclusion_reasons=(
                    _reason(
                        "EXACT_DUPLICATE_NOT_SELECTED",
                        f"equivalent occurrence {chosen.key} was selected",
                    ),
                ),
                selected_fact_key=chosen.key,
            )

    ordered_statuses = tuple(
        statuses[observation.key]
        for observation in sorted(bundle.observations, key=lambda item: item.source_order)
    )
    if len(ordered_statuses) != len(bundle.observations):
        raise ValueError("Every observation must receive exactly one report role.")
    return ReportEvaluation(
        report_kind=report_kind,
        rule_version=rule_version,
        report_date=bundle.filing.report_date,
        statuses=ordered_statuses,
        reconciliation_issues=tuple(issues),
    )
