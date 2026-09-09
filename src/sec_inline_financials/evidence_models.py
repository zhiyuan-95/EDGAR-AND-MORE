from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Literal

from sec_inline_financials.models import Company, Filing

ReportKind = Literal["annual", "quarterly"]
EvidenceRole = Literal[
    "selected_primary",
    "dimensional",
    "duplicate_not_selected",
    "conflict_candidate",
    "excluded",
]


@dataclass(frozen=True)
class SourceLocator:
    document_key: str | None
    node_path: str
    line: int | None = None


@dataclass(frozen=True)
class SourceDocumentRecord:
    key: str
    original_uri: str
    document_kind: str
    retention_kind: Literal[
        "retained_original", "external_dependency_reference", "generated_document"
    ]
    captured_path: str | None = None
    content_hash: str | None = None
    byte_size: int | None = None
    media_type: str | None = None
    parent_uri: str | None = None
    source_reference: str | None = None


@dataclass(frozen=True)
class ConceptRecord:
    key: str
    namespace_uri: str
    local_name: str
    display_qname: str
    definition_status: str
    data_type_namespace_uri: str | None = None
    data_type_local_name: str | None = None
    period_type: str | None = None
    balance: str | None = None
    is_numeric: bool | None = None
    is_abstract: bool | None = None
    source_document_key: str | None = None
    source_locator: str | None = None


@dataclass(frozen=True)
class ConceptLabelRecord:
    concept_key: str
    role_uri: str
    language: str
    label_text: str
    source_order: int
    source_document_key: str | None = None
    source_locator: str | None = None


@dataclass(frozen=True)
class ContextDimensionRecord:
    source_order: int
    axis_namespace_uri: str
    axis_local_name: str
    member_kind: Literal["explicit", "typed", "unresolved"]
    context_element: str
    axis_concept_key: str | None = None
    member_concept_key: str | None = None
    explicit_member_namespace_uri: str | None = None
    explicit_member_local_name: str | None = None
    typed_xml: str | None = None
    typed_text: str | None = None
    typed_hash: str | None = None


@dataclass(frozen=True)
class ContextRecord:
    key: str
    source_order: int
    source_document_key: str | None
    source_locator: str
    raw_xml: str
    period_kind: Literal["instant", "duration", "forever", "unknown"]
    xml_id: str | None = None
    entity_scheme: str | None = None
    entity_identifier: str | None = None
    raw_start: str | None = None
    raw_end: str | None = None
    raw_instant: str | None = None
    period_start_date: date | None = None
    period_end_date: date | None = None
    exclusive_end: str | None = None
    segment_xml: str | None = None
    scenario_xml: str | None = None
    canonical_hash: str | None = None
    legacy_report_dimensions: tuple[str, ...] = ()
    dimensions: tuple[ContextDimensionRecord, ...] = ()


@dataclass(frozen=True)
class UnitMeasureRecord:
    side: Literal["numerator", "denominator"]
    measure_order: int
    namespace_uri: str
    local_name: str
    display_qname: str


@dataclass(frozen=True)
class UnitRecord:
    key: str
    source_order: int
    source_document_key: str | None
    source_locator: str
    raw_xml: str
    xml_id: str | None = None
    legacy_report_unit_text: str | None = None
    canonical_hash: str | None = None
    measures: tuple[UnitMeasureRecord, ...] = ()


@dataclass(frozen=True)
class ObservationRecord:
    key: str
    source_order: int
    observation_origin: Literal["recognized", "undefined"]
    fact_kind: Literal["item", "tuple", "unresolved"]
    source_document_key: str | None
    source_locator: str
    is_nil: bool
    validity_code: int | None
    validity_name: str
    concept_key: str | None = None
    display_qname: str | None = None
    context_key: str | None = None
    unit_key: str | None = None
    raw_context_ref: str | None = None
    raw_unit_ref: str | None = None
    parent_fact_key: str | None = None
    xml_id: str | None = None
    source_line: int | None = None
    top_level_order: int | None = None
    raw_value_text: str | None = None
    typed_value_kind: str | None = None
    typed_value_text: str | None = None
    is_numeric: bool | None = None
    numeric_conversion_error: str | None = None
    decimals: str | None = None
    precision: str | None = None
    language: str | None = None
    inline_metadata_json: str | None = None
    legacy_report_label_text: str | None = None


@dataclass(frozen=True)
class ExtractionDiagnosticRecord:
    code: str
    severity: str
    message: str
    source_order: int
    fact_key: str | None = None
    context_key: str | None = None
    unit_key: str | None = None
    source_document_key: str | None = None
    raw_details_json: str | None = None


@dataclass(frozen=True)
class ValidationReferenceRecord:
    reference_order: int
    raw_reference_json: str
    resolution_status: Literal["resolved", "ambiguous", "unresolved"]
    fact_key: str | None = None
    source_document_key: str | None = None
    source_locator: str | None = None


@dataclass(frozen=True)
class ValidationRecord:
    message_order: int
    level: str
    code: str
    message_text: str
    raw_record_json: str
    references: tuple[ValidationReferenceRecord, ...] = ()


@dataclass(frozen=True)
class CalculationNetworkRecord:
    arcrole_uri: str
    extraction_status: Literal["extracted", "extracted_empty", "extraction_failed"]
    relationship_count: int


@dataclass(frozen=True)
class CalculationRelationshipRecord:
    arcrole_uri: str
    role_uri: str
    relationship_order: int
    parent_concept_key: str
    child_concept_key: str
    exact_weight_text: str
    exact_order_text: str | None = None
    role_definition: str | None = None
    link_namespace_uri: str | None = None
    link_local_name: str | None = None
    arc_namespace_uri: str | None = None
    arc_local_name: str | None = None
    source_document_key: str | None = None
    source_locator: str | None = None
    raw_arc_details_json: str | None = None


@dataclass(frozen=True)
class CoverageManifest:
    recognized_fact_count: int
    unresolved_observation_count: int
    numeric_count: int
    nonnumeric_count: int
    nil_count: int
    invalid_count: int
    context_count: int
    unit_count: int
    validation_message_count: int
    calculation_relationship_count: int
    source_document_count: int


@dataclass(frozen=True)
class ExtractionProfile:
    application_version: str
    extractor_version: str
    arelle_version: str
    validation_options: tuple[tuple[str, str], ...]
    transform_plugin_revision: str
    transform_plugin_hashes: tuple[tuple[str, str], ...]
    serialization_version: str = "evidence-v1"


@dataclass(frozen=True)
class FilingEvidenceBundle:
    company: Company
    filing: Filing
    captured_company_name: str
    captured_company_ticker: str | None
    fiscal_year: int | None
    fiscal_period: str | None
    fiscal_year_source: str | None
    fiscal_period_source: str | None
    source_documents: tuple[SourceDocumentRecord, ...]
    concepts: tuple[ConceptRecord, ...]
    concept_labels: tuple[ConceptLabelRecord, ...]
    contexts: tuple[ContextRecord, ...]
    units: tuple[UnitRecord, ...]
    observations: tuple[ObservationRecord, ...]
    diagnostics: tuple[ExtractionDiagnosticRecord, ...]
    validation_messages: tuple[ValidationRecord, ...]
    calculation_networks: tuple[CalculationNetworkRecord, ...]
    calculation_relationships: tuple[CalculationRelationshipRecord, ...]
    extraction_profile: ExtractionProfile
    coverage_manifest: CoverageManifest
    raw_log_json: str


@dataclass(frozen=True)
class FactReportStatus:
    fact_key: str
    evidence_role: EvidenceRole
    exclusion_reasons: tuple[tuple[str, str], ...] = ()
    selection_note: str | None = None
    selected_fact_key: str | None = None


@dataclass(frozen=True)
class ReconciliationIssueRecord:
    concept_key: str
    reason_code: str
    reason_text: str
    issue_order: int
    candidate_fact_keys: tuple[str, ...]


@dataclass(frozen=True)
class ReportEvaluation:
    report_kind: ReportKind
    rule_version: str
    report_date: date
    statuses: tuple[FactReportStatus, ...]
    reconciliation_issues: tuple[ReconciliationIssueRecord, ...] = ()


@dataclass(frozen=True)
class InstalledArtifact:
    sha256: str
    relative_object_path: str
    byte_size: int
    media_type: str
    purpose: str
    logical_name: str
    source_document_key: str | None = None


@dataclass(frozen=True)
class SnapshotRef:
    snapshot_id: int
    filing_id: int
    accession: str
    source_manifest_hash: str
    extraction_profile_hash: str
    payload_hash: str


@dataclass(frozen=True)
class StoreResult:
    snapshot_id: int
    disposition: Literal["stored", "reused"]


@dataclass(frozen=True)
class ReportEvaluationRef:
    evaluation_id: int
    snapshot_id: int
    report_kind: ReportKind
    rule_version: str


@dataclass(frozen=True)
class FactQuery:
    concept_namespace_uri: str | None = None
    concept_local_name: str | None = None
    period_start_date: date | None = None
    period_end_date: date | None = None
    evidence_role: EvidenceRole | None = None
    evaluation_id: int | None = None


@dataclass(frozen=True)
class Page:
    items: tuple[dict[str, object], ...]
    next_cursor: int | None


@dataclass(frozen=True)
class FilingOutcome:
    accession: str
    status: Literal["stored", "reused", "failed"]
    snapshot_id: int | None = None
    error: str | None = None


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    status: Literal["succeeded", "partial", "failed"]
    filings: tuple[FilingOutcome, ...] = field(default_factory=tuple)
