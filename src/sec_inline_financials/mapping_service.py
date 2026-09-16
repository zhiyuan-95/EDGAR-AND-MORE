from __future__ import annotations

from dataclasses import replace

from sec_inline_financials.direct_mapping import resolve_metric_window
from sec_inline_financials.errors import MappingInputError
from sec_inline_financials.evidence_models import ReportKind
from sec_inline_financials.mapping_models import (
    CompanyMappingResult,
    DirectMappingRuleSet,
    MappingSnapshotInput,
    MetricEvaluationRef,
    active_window_hash,
)
from sec_inline_financials.mapping_rules import DIRECT_MAPPING_RULES
from sec_inline_financials.storage.evidence_store import EvidenceStore


class DirectMappingService:
    """Evaluate and publish Direct Mapping entirely from retained evidence."""

    def __init__(
        self,
        store: EvidenceStore,
        *,
        rule_set: DirectMappingRuleSet = DIRECT_MAPPING_RULES,
    ) -> None:
        self._store = store
        self._rule_set = rule_set

    def evaluate_company(
        self,
        ticker: str,
        *,
        require_complete_window: bool = True,
    ) -> CompanyMappingResult:
        self._store.initialize()
        state = self._store.get_company_state(ticker)
        if state is None:
            raise MappingInputError(f"No stored company data found for {ticker.strip().upper()}.")
        annual_inputs = self._store.list_mapping_inputs(ticker, "annual", "report-v1")
        quarterly_inputs = self._store.list_mapping_inputs(ticker, "quarterly", "report-v1")
        inputs = {"annual": annual_inputs, "quarterly": quarterly_inputs}
        if require_complete_window:
            expected = {"annual": 5, "quarterly": 12}
            for kind, count in expected.items():
                actual = len(inputs[kind])
                if actual != count:
                    raise MappingInputError(
                        f"Direct Mapping requires {count} active {kind} filings; found {actual}."
                    )
        annual = self._evaluate_kind(ticker, "annual", inputs["annual"])
        quarterly = self._evaluate_kind(ticker, "quarterly", inputs["quarterly"])
        return CompanyMappingResult(company=state.company, annual=annual, quarterly=quarterly)

    def _evaluate_kind(
        self,
        ticker: str,
        report_kind: ReportKind,
        snapshots: tuple[MappingSnapshotInput, ...],
    ) -> MetricEvaluationRef:
        window_hash = active_window_hash(snapshots)
        existing = self._store.find_metric_evaluation(
            ticker,
            report_kind,
            definition_version=self._rule_set.definition_version,
            mapping_rule_hash=self._rule_set.canonical_hash,
            source_report_rule_version=snapshots[0].source_report_rule_version,
            active_window_hash_value=window_hash,
        )
        if existing is not None:
            self._store.publish_metric_evaluation(
                ticker, existing.evaluation_id, expected_active_window_hash=window_hash
            )
            return replace(existing, reused=True, stale=False)

        candidates = tuple(
            candidate for rule in self._rule_set.metrics for candidate in rule.candidates
        )
        facts = self._store.load_mapping_candidate_facts(snapshots, candidates)
        evaluation = resolve_metric_window(
            rule_set=self._rule_set,
            report_kind=report_kind,
            snapshots=snapshots,
            observed_concepts=frozenset(fact.concept for fact in facts),
            candidate_facts=facts,
        )
        return self._store.save_and_publish_metric_evaluation(
            ticker,
            evaluation,
            expected_active_window_hash=window_hash,
        )
