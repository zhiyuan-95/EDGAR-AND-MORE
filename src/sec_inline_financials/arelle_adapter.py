from __future__ import annotations

import json
import os
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Literal, overload

from arelle import XbrlConst
from arelle.api.Session import Session
from arelle.RuntimeOptions import RuntimeOptions
from arelle.XmlValidateConst import VALID

from sec_inline_financials.errors import ProcessingError
from sec_inline_financials.evidence_extraction import (
    build_extraction_profile,
    detach_filing_evidence,
)
from sec_inline_financials.evidence_models import ExtractionProfile, FilingEvidenceBundle
from sec_inline_financials.models import (
    AnnualResult,
    CalculationRelationship,
    Company,
    Fact,
    Filing,
    QuarterlyResult,
    ValidationMessage,
)
from sec_inline_financials.reconcile import reconcile_primary_facts
from sec_inline_financials.sec_transform_plugin import ensure_sec_transform_plugin


class ArelleProcessor:
    """Load one Inline XBRL filing and detach its report-period evidence from Arelle."""

    def __init__(self, *, user_agent: str, cache_directory: Path | None = None) -> None:
        self._user_agent = user_agent
        self._cache_directory = cache_directory

    def process(self, filing: Filing) -> AnnualResult:
        return self._process(filing, period_kind="annual")

    def process_quarterly(self, filing: Filing) -> QuarterlyResult:
        return self._process(filing, period_kind="quarterly")

    def extract_evidence(
        self, company: Company, filing: Filing, capture_area: Path
    ) -> FilingEvidenceBundle:
        """Capture and detach complete filing evidence while Arelle is still open."""
        plugin_cache = (
            self._cache_directory.parent
            if self._cache_directory is not None
            else capture_area.parent
        )
        sec_transform_plugin = ensure_sec_transform_plugin(plugin_cache)
        attempt_cache = capture_area / "arelle-cache"
        options = RuntimeOptions(
            entrypointFile=filing.url,
            internetConnectivity="online",
            internetTimeout=60,
            httpUserAgent=self._user_agent,
            cacheDirectory=str(attempt_cache),
            keepOpen=True,
            validate=True,
            calcs="c10d",
            validateDuplicateFacts="all",
            plugins=str(sec_transform_plugin),
            logFile="logToBuffer",
            logLevel="WARNING",
            logTextMaxLength=10_000,
            disablePersistentConfig=True,
        )
        with Session() as session:
            succeeded = session.run(options)
            models = session.get_models()
            if not succeeded or not models:
                raise ProcessingError(f"Arelle could not load {filing.accession} ({filing.url}).")
            raw_log_json = session.get_logs("json")
            capture_area.mkdir(parents=True, exist_ok=True)
            raw_log_path = capture_area / "raw-arelle-log.json"
            with raw_log_path.open("xb") as stream:
                stream.write(raw_log_json.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            return detach_filing_evidence(
                models[0],
                company=company,
                filing=filing,
                raw_log_json=raw_log_json,
                capture_area=capture_area / "captured-sources",
                extraction_profile=build_extraction_profile(sec_transform_plugin),
            )

    def extraction_profile(self) -> ExtractionProfile:
        plugin_cache = (
            self._cache_directory.parent
            if self._cache_directory is not None
            else Path.cwd() / ".cache"
        )
        return build_extraction_profile(ensure_sec_transform_plugin(plugin_cache))

    @overload
    def _process(self, filing: Filing, *, period_kind: Literal["annual"]) -> AnnualResult: ...

    @overload
    def _process(self, filing: Filing, *, period_kind: Literal["quarterly"]) -> QuarterlyResult: ...

    def _process(
        self, filing: Filing, *, period_kind: Literal["annual", "quarterly"]
    ) -> AnnualResult | QuarterlyResult:
        plugin_cache = (
            self._cache_directory.parent
            if self._cache_directory is not None
            else Path.cwd() / ".cache"
        )
        sec_transform_plugin = ensure_sec_transform_plugin(plugin_cache)
        options = RuntimeOptions(
            entrypointFile=filing.url,
            internetConnectivity="online",
            internetTimeout=60,
            httpUserAgent=self._user_agent,
            cacheDirectory=str(self._cache_directory) if self._cache_directory else None,
            keepOpen=True,
            validate=True,
            calcs="c10d",
            validateDuplicateFacts="all",
            plugins=str(sec_transform_plugin),
            logFile="logToBuffer",
            logLevel="WARNING",
            logTextMaxLength=10_000,
            disablePersistentConfig=True,
        )
        with Session() as session:
            succeeded = session.run(options)
            models = session.get_models()
            if not succeeded or not models:
                raise ProcessingError(f"Arelle could not load {filing.accession} ({filing.url}).")
            model = models[0]
            facts = self._numeric_facts(model, filing.report_date, period_kind=period_kind)
            primary_candidates = tuple(fact for fact in facts if not fact.dimensions)
            dimensional_facts = tuple(fact for fact in facts if fact.dimensions)
            reconciliation = reconcile_primary_facts(primary_candidates)
            fiscal_year = self._fiscal_year(model, filing)
            fiscal_period = self._fiscal_period(model, filing) if period_kind == "quarterly" else ""
            messages = self._validation_messages(session.get_logs("json"))
            calculations = self._calculation_relationships(model)

        if not reconciliation.selected and not dimensional_facts:
            raise ProcessingError(
                f"Arelle found no valid {period_kind} numeric facts for {filing.accession}."
            )
        if period_kind == "quarterly":
            return QuarterlyResult(
                fiscal_year=fiscal_year,
                fiscal_period=fiscal_period,
                filing=filing,
                primary_facts=reconciliation.selected,
                dimensional_facts=dimensional_facts,
                validation_messages=messages,
                calculation_relationships=calculations,
                reconciliation_issues=reconciliation.issues,
            )
        return AnnualResult(
            fiscal_year=fiscal_year,
            filing=filing,
            primary_facts=reconciliation.selected,
            dimensional_facts=dimensional_facts,
            validation_messages=messages,
            calculation_relationships=calculations,
            reconciliation_issues=reconciliation.issues,
        )

    @staticmethod
    def _fiscal_year(model: Any, filing: Filing) -> int:
        for fact in model.facts:
            if str(getattr(fact, "qname", "")) != "dei:DocumentFiscalYearFocus":
                continue
            try:
                return int(fact.xValue)
            except (TypeError, ValueError):
                continue
        return filing.report_date.year

    @staticmethod
    def _fiscal_period(model: Any, filing: Filing) -> str:
        for fact in model.facts:
            if str(getattr(fact, "qname", "")) != "dei:DocumentFiscalPeriodFocus":
                continue
            fiscal_period = str(getattr(fact, "xValue", "")).upper()
            if fiscal_period in {"Q1", "Q2", "Q3"}:
                return fiscal_period
        return f"ended-{filing.report_date.isoformat()}"

    @staticmethod
    def _numeric_facts(
        model: Any,
        report_date: date,
        *,
        period_kind: Literal["annual", "quarterly"],
    ) -> tuple[Fact, ...]:
        normalized: list[Fact] = []
        for model_fact in model.facts:
            if not getattr(model_fact, "isNumeric", False):
                continue
            if getattr(model_fact, "isNil", False) or getattr(model_fact, "xValid", 0) < VALID:
                continue
            context = getattr(model_fact, "context", None)
            unit = getattr(model_fact, "unit", None)
            if context is None or unit is None:
                continue
            if period_kind == "annual":
                period = _annual_period(context, report_date)
            else:
                period = _quarterly_period(context, report_date)
            if period is None:
                continue
            period_start, period_end = period
            try:
                value = Decimal(str(model_fact.xValue))
            except (InvalidOperation, TypeError, ValueError):
                continue
            if not value.is_finite():
                continue
            concept = getattr(model_fact, "concept", None)
            label = None
            if concept is not None:
                label = concept.label(fallbackToQname=True, lang="en", strip=True)
            qname = str(model_fact.qname)
            normalized.append(
                Fact(
                    concept=qname,
                    label=str(label or qname),
                    value=value,
                    raw_value=str(model_fact.value),
                    period_start=period_start,
                    period_end=period_end,
                    unit=_unit_text(unit),
                    decimals=(
                        str(model_fact.decimals)
                        if getattr(model_fact, "decimals", None) is not None
                        else None
                    ),
                    dimensions=_dimension_texts(context),
                    arelle_validity="valid",
                    context_id=str(getattr(context, "id", "")),
                    reconciliation_note=(f"selected unique dimension-free {period_kind} fact"),
                )
            )
        return tuple(normalized)

    @staticmethod
    def _validation_messages(log_json: str) -> tuple[ValidationMessage, ...]:
        try:
            payload = json.loads(log_json)
        except (json.JSONDecodeError, TypeError):
            return ()
        records = payload.get("log", []) if isinstance(payload, dict) else []
        messages: list[ValidationMessage] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            level = str(record.get("level", "unknown"))
            code = str(record.get("code", "arelle"))
            message_value = record.get("message", "")
            if isinstance(message_value, dict):
                text = str(message_value.get("text", ""))
            else:
                text = str(message_value)
            messages.append(
                ValidationMessage(level=level, code=code, message=" ".join(text.split()))
            )
        return tuple(messages)

    @staticmethod
    def _calculation_relationships(model: Any) -> tuple[CalculationRelationship, ...]:
        relationship_set = model.relationshipSet(XbrlConst.summationItem)
        relationships: set[CalculationRelationship] = set()
        for relationship in relationship_set.modelRelationships:
            parent = getattr(relationship, "fromModelObject", None)
            child = getattr(relationship, "toModelObject", None)
            if parent is None or child is None:
                continue
            try:
                weight = Decimal(str(relationship.weight))
            except (InvalidOperation, TypeError, ValueError):
                continue
            relationships.add(
                CalculationRelationship(
                    role=str(getattr(relationship, "linkrole", "")),
                    parent=str(parent.qname),
                    child=str(child.qname),
                    weight=weight,
                )
            )
        return tuple(
            sorted(
                relationships,
                key=lambda item: (item.role, item.parent.casefold(), item.child.casefold()),
            )
        )


def _annual_period(context: Any, report_date: date) -> tuple[date | None, date] | None:
    end_datetime = getattr(context, "endDatetime", None)
    if end_datetime is None:
        return None
    actual_end = (end_datetime - timedelta(days=1)).date()
    if actual_end != report_date:
        return None
    if getattr(context, "isInstantPeriod", False):
        return None, actual_end
    if not getattr(context, "isStartEndPeriod", False):
        return None
    start_datetime = getattr(context, "startDatetime", None)
    if start_datetime is None:
        return None
    duration_days = (end_datetime - start_datetime).days
    if not 300 <= duration_days <= 400:
        return None
    return start_datetime.date(), actual_end


def _quarterly_period(context: Any, report_date: date) -> tuple[date | None, date] | None:
    end_datetime = getattr(context, "endDatetime", None)
    if end_datetime is None:
        return None
    actual_end = (end_datetime - timedelta(days=1)).date()
    if actual_end != report_date:
        return None
    if getattr(context, "isInstantPeriod", False):
        return None, actual_end
    if not getattr(context, "isStartEndPeriod", False):
        return None
    start_datetime = getattr(context, "startDatetime", None)
    if start_datetime is None:
        return None
    duration_days = (end_datetime - start_datetime).days
    if not 60 <= duration_days <= 120:
        return None
    return start_datetime.date(), actual_end


def _unit_text(unit: Any) -> str:
    numerator, denominator = unit.measures
    numerator_text = "*".join(str(measure) for measure in numerator) or "1"
    denominator_text = "*".join(str(measure) for measure in denominator)
    if denominator_text:
        return f"{numerator_text}/{denominator_text}"
    return numerator_text


def _dimension_texts(context: Any) -> tuple[str, ...]:
    dimensions: list[str] = []
    for dimension_qname, value in sorted(
        context.qnameDims.items(), key=lambda item: str(item[0]).casefold()
    ):
        member_qname = getattr(value, "memberQname", None)
        if member_qname is not None:
            dimensions.append(f"{dimension_qname}={member_qname}")
            continue
        typed_member = getattr(value, "typedMember", None)
        dimensions.append(f"{dimension_qname}={typed_member if typed_member is not None else '?'}")
    return tuple(dimensions)
