from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from typing import Literal

from sec_inline_financials.evidence_models import EvidenceRole, ReportKind
from sec_inline_financials.models import Company

PeriodType = Literal["duration", "instant"]
UnitFamily = Literal["monetary"]
MetricResultStatus = Literal["reported", "missing"]
MissingReason = Literal["mapping_not_found", "no_selectable_fact_for_period"]

TARGET_METRIC_KEYS = (
    "revenue",
    "operating_income",
    "net_income",
    "total_assets",
    "total_liabilities",
    "equity",
    "operating_cash_flow",
)
TARGET_METRIC_PERIOD_TYPES: dict[str, PeriodType] = {
    "revenue": "duration",
    "operating_income": "duration",
    "net_income": "duration",
    "total_assets": "instant",
    "total_liabilities": "instant",
    "equity": "instant",
    "operating_cash_flow": "duration",
}


@dataclass(frozen=True)
class ConceptCandidate:
    namespace_family: str
    local_name: str
    rank: int


@dataclass(frozen=True)
class MetricRule:
    metric_key: str
    display_name: str
    expected_period_type: PeriodType
    expected_unit_family: UnitFamily
    candidates: tuple[ConceptCandidate, ...]


@dataclass(frozen=True)
class DirectMappingRuleSet:
    definition_version: str
    mapping_rule_version: str
    metrics: tuple[MetricRule, ...]

    def __post_init__(self) -> None:
        if not self.definition_version or not self.mapping_rule_version:
            raise ValueError("Direct Mapping version identifiers must not be empty.")
        keys = tuple(rule.metric_key for rule in self.metrics)
        if keys != TARGET_METRIC_KEYS:
            raise ValueError("Direct Mapping rules must define the seven Target Metrics in order.")
        for rule in self.metrics:
            if not rule.metric_key or not rule.display_name:
                raise ValueError("Metric keys and display names must not be empty.")
            if not rule.candidates:
                raise ValueError(f"Metric {rule.metric_key} requires at least one candidate.")
            if rule.expected_period_type != TARGET_METRIC_PERIOD_TYPES[rule.metric_key]:
                raise ValueError(
                    f"Metric {rule.metric_key} has the wrong Target Metric period type."
                )
            ranks = tuple(candidate.rank for candidate in rule.candidates)
            if ranks != tuple(range(len(rule.candidates))):
                raise ValueError(
                    f"Metric {rule.metric_key} candidate ranks must be consecutive from zero."
                )
            identities = tuple(
                (candidate.namespace_family, candidate.local_name) for candidate in rule.candidates
            )
            if len(identities) != len(set(identities)):
                raise ValueError(f"Metric {rule.metric_key} contains a duplicate candidate.")
            if any(not family or not local_name for family, local_name in identities):
                raise ValueError("Candidate namespace families and local names must not be empty.")

    @property
    def canonical_json(self) -> str:
        value = {
            "definition_version": self.definition_version,
            "mapping_rule_version": self.mapping_rule_version,
            "metrics": [
                {
                    "metric_key": rule.metric_key,
                    "display_name": rule.display_name,
                    "expected_period_type": rule.expected_period_type,
                    "expected_unit_family": rule.expected_unit_family,
                    "candidates": [
                        {
                            "namespace_family": candidate.namespace_family,
                            "local_name": candidate.local_name,
                            "rank": candidate.rank,
                        }
                        for candidate in rule.candidates
                    ],
                }
                for rule in self.metrics
            ],
        }
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @property
    def canonical_hash(self) -> str:
        return hashlib.sha256(self.canonical_json.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ConceptIdentity:
    namespace_family: str | None
    namespace_uri: str
    local_name: str
    display_qname: str


@dataclass(frozen=True)
class MappingSnapshotInput:
    snapshot_id: int
    filing_id: int
    accession: str
    form: str
    report_date: date
    active_window_rank: int
    payload_hash: str
    report_evaluation_id: int
    source_report_rule_version: str
    company_cik: str


@dataclass(frozen=True)
class MappingFactInput:
    snapshot_id: int
    fact_id: int
    concept: ConceptIdentity
    concept_period_type: str | None
    context_period_kind: str
    entity_identifier: str | None
    period_start_date: date | None
    period_end_date: date | None
    unit_family: str | None
    typed_value_text: str | None
    evidence_role: EvidenceRole
    exclusion_reasons: tuple[tuple[str, str], ...] = ()
    conflict_reason_codes: tuple[str, ...] = ()


@dataclass(frozen=True)
class MetricResult:
    metric_key: str
    snapshot_id: int
    report_evaluation_id: int
    status: MetricResultStatus
    missing_reason: MissingReason | None
    selected_candidate_rank: int | None
    selected_fact_id: int | None
    typed_value_text: str | None
    resolution_trace_json: str


@dataclass(frozen=True)
class MetricWindowEvaluation:
    report_kind: ReportKind
    definition_version: str
    mapping_rule_version: str
    mapping_rule_hash: str
    mapping_rule_json: str
    source_report_rule_version: str
    snapshots: tuple[MappingSnapshotInput, ...]
    results: tuple[MetricResult, ...]


@dataclass(frozen=True)
class MetricEvaluationRef:
    evaluation_id: int
    company_id: int
    report_kind: ReportKind
    definition_version: str
    mapping_rule_version: str
    mapping_rule_hash: str
    source_report_rule_version: str
    active_window_hash: str
    reported_count: int
    missing_count: int
    reused: bool
    stale: bool


@dataclass(frozen=True)
class CompanyMappingResult:
    company: Company
    annual: MetricEvaluationRef | None
    quarterly: MetricEvaluationRef | None
    annual_error: str | None = None
    quarterly_error: str | None = None


def active_window_hash(snapshots: tuple[MappingSnapshotInput, ...]) -> str:
    records = [
        {
            "form": snapshot.form,
            "active_window_rank": snapshot.active_window_rank,
            "filing_id": snapshot.filing_id,
            "snapshot_id": snapshot.snapshot_id,
            "payload_hash": snapshot.payload_hash,
            "report_evaluation_id": snapshot.report_evaluation_id,
        }
        for snapshot in sorted(snapshots, key=lambda item: item.active_window_rank)
    ]
    serialized = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
