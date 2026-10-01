from __future__ import annotations

import re
from collections import defaultdict
from decimal import Decimal, InvalidOperation

from arelle.XmlValidateConst import VALID

from sec_inline_financials.company_lineage import ReportingTransition
from sec_inline_financials.evidence_models import (
    ContextDimensionRecord,
    ContextRecord,
    FactReportStatus,
    FilingEvidenceBundle,
    ObservationRecord,
    ReconciliationIssueRecord,
    ReportEvaluation,
    ReportKind,
)

LEGACY_REPORT_RULE_VERSION = "report-v1"
REPORT_RULE_VERSION = "report-v2"
_DEI_NAMESPACE = re.compile(r"https?://xbrl\.sec\.gov/dei/\d{4}")


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
    rule_version: str,
    transition_reason: tuple[str, str] | None,
) -> tuple[tuple[str, str], ...]:
    contexts = {context.key: context for context in bundle.contexts}
    units = {unit.key: unit for unit in bundle.units}
    reasons: list[tuple[str, str]] = []
    if observation.top_level_order is None:
        reasons.append(
            _reason(
                "LEGACY_REPORT_TOP_LEVEL_ONLY",
                f"{rule_version} includes only the existing top-level Arelle fact traversal",
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
        if transition_reason is not None:
            reasons.append(transition_reason)
    if observation.is_numeric and observation.unit_key not in units:
        reasons.append(_reason("MISSING_UNIT", "unitRef did not resolve uniquely"))
    return tuple(reasons)


def _normalized_cik(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped.isdigit() or len(stripped) > 10:
        return None
    return stripped.zfill(10)


def _normalized_legal_name(value: str, *, member: bool = False) -> str:
    normalized = "".join(character.lower() for character in value if character.isalnum())
    if member and normalized.endswith("member"):
        normalized = normalized[: -len("member")]
    return normalized


def _is_predecessor_identity_dimension(
    dimension: ContextDimensionRecord,
    transition: ReportingTransition,
) -> bool:
    return (
        dimension.member_kind == "explicit"
        and bool(_DEI_NAMESPACE.fullmatch(dimension.axis_namespace_uri.rstrip("/")))
        and dimension.axis_local_name == "LegalEntityAxis"
        and dimension.explicit_member_local_name is not None
        and _normalized_legal_name(dimension.explicit_member_local_name, member=True)
        == _normalized_legal_name(transition.predecessor.legal_name)
    )


def _transition_context(
    context: ContextRecord,
    transition: ReportingTransition | None,
) -> tuple[tuple[ContextDimensionRecord, ...], bool, tuple[str, str] | None]:
    if transition is None:
        return context.dimensions, False, None
    entity_cik = _normalized_cik(context.entity_identifier)
    predecessor_cik = _normalized_cik(transition.predecessor.cik)
    successor_cik = _normalized_cik(transition.successor.cik)
    if entity_cik == predecessor_cik:
        return context.dimensions, False, None
    matching_dimensions = tuple(
        dimension
        for dimension in context.dimensions
        if _is_predecessor_identity_dimension(dimension, transition)
    )
    if entity_cik == successor_cik and len(matching_dimensions) == 1:
        identity_dimension = matching_dimensions[0]
        return (
            tuple(
                dimension for dimension in context.dimensions if dimension is not identity_dimension
            ),
            True,
            None,
        )
    return (
        context.dimensions,
        False,
        _reason(
            "TRANSITION_NON_REPORTING_ENTITY",
            "context does not identify the verified predecessor for this pre-transition period",
        ),
    )


def classify_report(
    bundle: FilingEvidenceBundle,
    report_kind: ReportKind,
    rule_version: str = REPORT_RULE_VERSION,
    *,
    transition: ReportingTransition | None = None,
) -> ReportEvaluation:
    """Classify facts while retaining every observed filing fact as evidence."""
    if rule_version not in {LEGACY_REPORT_RULE_VERSION, REPORT_RULE_VERSION}:
        raise ValueError(f"Unsupported report rule version: {rule_version}")
    active_transition = transition
    if (
        rule_version == LEGACY_REPORT_RULE_VERSION
        or transition is None
        or bundle.filing.report_date >= transition.effective_date
        or _normalized_cik(bundle.filing.registrant_cik)
        != _normalized_cik(transition.successor.cik)
    ):
        active_transition = None
    contexts = {context.key: context for context in bundle.contexts}
    units = {unit.key: unit for unit in bundle.units}
    statuses: dict[str, FactReportStatus] = {}
    primary_by_concept: dict[str, list[ObservationRecord]] = defaultdict(list)
    transition_wrapper_removed: dict[str, bool] = {}

    for observation in sorted(bundle.observations, key=lambda item: item.source_order):
        context = contexts.get(observation.context_key or "")
        effective_dimensions: tuple[ContextDimensionRecord, ...] = ()
        wrapper_removed = False
        transition_reason = None
        if context is not None:
            effective_dimensions, wrapper_removed, transition_reason = _transition_context(
                context, active_transition
            )
        reasons = _eligibility_reasons(
            observation,
            bundle,
            report_kind,
            rule_version,
            transition_reason,
        )
        if reasons:
            statuses[observation.key] = FactReportStatus(
                fact_key=observation.key,
                evidence_role="excluded",
                exclusion_reasons=reasons,
            )
            continue
        context = contexts[observation.context_key or ""]
        if effective_dimensions:
            statuses[observation.key] = FactReportStatus(
                fact_key=observation.key,
                evidence_role="dimensional",
            )
            continue
        transition_wrapper_removed[observation.key] = wrapper_removed
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
        if transition_wrapper_removed.get(chosen.key, False):
            note += "; removed verified transition LegalEntityAxis wrapper"
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
