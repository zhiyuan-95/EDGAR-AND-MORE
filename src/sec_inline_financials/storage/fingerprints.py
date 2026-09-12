from __future__ import annotations

import hashlib
import json
from dataclasses import fields, is_dataclass
from datetime import date, datetime
from enum import Enum
from typing import Any

from sec_inline_financials.evidence_models import FilingEvidenceBundle


def canonical_json(value: object) -> str:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def extraction_profile_json(bundle: FilingEvidenceBundle) -> str:
    return canonical_json(bundle.extraction_profile)


def extraction_profile_hash(bundle: FilingEvidenceBundle) -> str:
    return sha256_text(extraction_profile_json(bundle))


def source_manifest_json(bundle: FilingEvidenceBundle) -> str:
    records = [
        {
            "document_key": document.key,
            "original_uri": document.original_uri,
            "document_kind": document.document_kind,
            "retention_kind": document.retention_kind,
            "content_hash": document.content_hash,
            "hash_state": "verified" if document.content_hash else "unavailable",
            "byte_size": document.byte_size,
            "parent_uri": document.parent_uri,
            "source_reference": document.source_reference,
        }
        for document in sorted(
            bundle.source_documents,
            key=lambda item: (item.original_uri, item.document_kind, item.key),
        )
    ]
    return canonical_json({"version": "source-manifest-v1", "documents": records})


def source_manifest_hash(bundle: FilingEvidenceBundle) -> str:
    return sha256_text(source_manifest_json(bundle))


def payload_json(bundle: FilingEvidenceBundle) -> str:
    serialization_version = bundle.extraction_profile.serialization_version
    validation = [
        {
            "message_order": record.message_order,
            "level": record.level,
            "code": record.code,
            "message_text": record.message_text,
            "references": [
                {
                    "reference_order": reference.reference_order,
                    "resolution_status": reference.resolution_status,
                    "fact_key": reference.fact_key,
                    "source_document_key": reference.source_document_key,
                    "source_locator": reference.source_locator,
                }
                for reference in record.references
            ],
        }
        for record in bundle.validation_messages
    ]
    source_identities = [
        {
            "key": document.key,
            "original_uri": document.original_uri,
            "document_kind": document.document_kind,
            "retention_kind": document.retention_kind,
            "parent_uri": document.parent_uri,
            "source_reference": document.source_reference,
        }
        for document in sorted(bundle.source_documents, key=lambda item: item.key)
    ]
    payload: dict[str, object] = {
        "version": (
            "evidence-payload-v2"
            if serialization_version == "evidence-v2"
            else "evidence-payload-v1"
        ),
        "filing": {
            "accession": bundle.filing.accession,
            "form": bundle.filing.form,
            "filing_date": bundle.filing.filing_date,
            "report_date": bundle.filing.report_date,
            "primary_document": bundle.filing.primary_document,
            "source_url": bundle.filing.url,
        },
        "fiscal_year": bundle.fiscal_year,
        "fiscal_period": bundle.fiscal_period,
        "fiscal_year_source": bundle.fiscal_year_source,
        "fiscal_period_source": bundle.fiscal_period_source,
        "source_documents": source_identities,
        "concepts": sorted(bundle.concepts, key=lambda item: item.key),
        "concept_labels": sorted(
            bundle.concept_labels,
            key=lambda item: (item.concept_key, item.source_order, item.role_uri, item.language),
        ),
        "contexts": [
            _semantic_record(item, "legacy_report_dimensions")
            for item in sorted(bundle.contexts, key=lambda item: item.key)
        ],
        "units": [
            _semantic_record(item, "legacy_report_unit_text")
            for item in sorted(bundle.units, key=lambda item: item.key)
        ],
        "observations": [
            _semantic_record(item, "legacy_report_label_text")
            for item in sorted(bundle.observations, key=lambda item: item.source_order)
        ],
        "diagnostics": sorted(bundle.diagnostics, key=lambda item: item.source_order),
        "validation_messages": validation,
        "calculation_networks": sorted(
            bundle.calculation_networks, key=lambda item: item.arcrole_uri
        ),
        "calculation_relationships": sorted(
            bundle.calculation_relationships,
            key=lambda item: (item.arcrole_uri, item.role_uri, item.relationship_order),
        ),
        "coverage_manifest": (
            bundle.coverage_manifest
            if serialization_version == "evidence-v2"
            else _v1_coverage_manifest(bundle)
        ),
    }
    if serialization_version == "evidence-v2":
        payload["filing_sections"] = sorted(
            bundle.filing_sections, key=lambda item: item.section_order
        )
    return canonical_json(payload)


def payload_hash(bundle: FilingEvidenceBundle) -> str:
    return sha256_text(payload_json(bundle))


def _normalize(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: _normalize(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {str(key): _normalize(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, bytes):
        return value.hex()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _semantic_record(value: object, *excluded_fields: str) -> dict[str, object]:
    normalized = _normalize(value)
    if not isinstance(normalized, dict):
        raise TypeError("Semantic fingerprint records must normalize to dictionaries.")
    for field_name in excluded_fields:
        normalized.pop(field_name, None)
    return normalized


def _v1_coverage_manifest(bundle: FilingEvidenceBundle) -> dict[str, int]:
    coverage = bundle.coverage_manifest
    return {
        "recognized_fact_count": coverage.recognized_fact_count,
        "unresolved_observation_count": coverage.unresolved_observation_count,
        "numeric_count": coverage.numeric_count,
        "nonnumeric_count": coverage.nonnumeric_count,
        "nil_count": coverage.nil_count,
        "invalid_count": coverage.invalid_count,
        "context_count": coverage.context_count,
        "unit_count": coverage.unit_count,
        "validation_message_count": coverage.validation_message_count,
        "calculation_relationship_count": coverage.calculation_relationship_count,
        "source_document_count": coverage.source_document_count,
    }
