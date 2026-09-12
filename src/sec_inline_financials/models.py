from __future__ import annotations
from dataclasses import dataclass
from datetime import date
from decimal import Decimal


@dataclass(frozen=True)
class Company:
    ticker: str
    cik: str
    name: str


@dataclass(frozen=True)
class Filing:
    accession: str
    filing_date: date
    report_date: date
    form: str
    primary_document: str
    url: str


@dataclass(frozen=True)
class Fact:
    concept: str
    label: str
    value: Decimal
    raw_value: str
    period_start: date | None
    period_end: date
    unit: str
    decimals: str | None
    dimensions: tuple[str, ...]
    arelle_validity: str
    context_id: str = ""
    reconciliation_note: str = "selected unique dimension-free annual fact"


@dataclass(frozen=True)
class ReconciliationIssue:
    concept: str
    reason: str
    candidate_context_ids: tuple[str, ...]
    candidate_summaries: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationMessage:
    level: str
    code: str
    message: str


@dataclass(frozen=True)
class CalculationRelationship:
    role: str
    parent: str
    child: str
    weight: Decimal


@dataclass(frozen=True)
class AnnualResult:
    fiscal_year: int
    filing: Filing
    primary_facts: tuple[Fact, ...]
    dimensional_facts: tuple[Fact, ...] = ()
    validation_messages: tuple[ValidationMessage, ...] = ()
    calculation_relationships: tuple[CalculationRelationship, ...] = ()
    reconciliation_issues: tuple[ReconciliationIssue, ...] = ()


@dataclass(frozen=True)
class QuarterlyResult:
    fiscal_year: int
    fiscal_period: str
    filing: Filing
    primary_facts: tuple[Fact, ...]
    dimensional_facts: tuple[Fact, ...] = ()
    validation_messages: tuple[ValidationMessage, ...] = ()
    calculation_relationships: tuple[CalculationRelationship, ...] = ()
    reconciliation_issues: tuple[ReconciliationIssue, ...] = ()


@dataclass(frozen=True)
class ReportData:
    company: Company
    annual_results: tuple[AnnualResult, ...]


@dataclass(frozen=True)
class QuarterlyReportData:
    company: Company
    quarterly_results: tuple[QuarterlyResult, ...]
