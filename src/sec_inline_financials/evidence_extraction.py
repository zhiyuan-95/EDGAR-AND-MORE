from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any, Literal

from arelle import XbrlConst
from arelle.ModelDocument import Type
from arelle.XmlValidateConst import INVALID, NONE, UNKNOWN, UNVALIDATED, VALID
from lxml import etree

from sec_inline_financials.errors import CaptureError, ExtractionError
from sec_inline_financials.evidence_models import (
    CalculationNetworkRecord,
    CalculationRelationshipRecord,
    ConceptLabelRecord,
    ConceptRecord,
    ContextDimensionRecord,
    ContextRecord,
    CoverageManifest,
    ExtractionDiagnosticRecord,
    ExtractionProfile,
    FilingEvidenceBundle,
    FilingSectionRecord,
    ObservationRecord,
    SourceDocumentRecord,
    UnitMeasureRecord,
    UnitRecord,
    ValidationRecord,
    ValidationReferenceRecord,
)
from sec_inline_financials.filing_sections import extract_filing_sections
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage.fingerprints import canonical_json

_VALIDITY_NAMES = {
    UNVALIDATED: "unvalidated",
    UNKNOWN: "unknown",
    INVALID: "invalid",
    NONE: "none",
    VALID: "valid",
    5: "valid-id",
    6: "valid-no-content",
}
_INLINE_ATTRIBUTES = ("scale", "sign", "format", "escape", "continuedAt", "footnoteRefs")


def build_extraction_profile(transform_plugin: Path) -> ExtractionProfile:
    try:
        application_version = version("sec-inline-financials")
    except PackageNotFoundError:
        application_version = "0.1.0"
    try:
        arelle_version = version("arelle-release")
    except PackageNotFoundError:
        arelle_version = "unknown"
    hashes: list[tuple[str, str]] = []
    paths = sorted(transform_plugin.rglob("*")) if transform_plugin.is_dir() else [transform_plugin]
    for path in paths:
        if (
            path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix.lower() not in {".pyc", ".pyo"}
        ):
            logical_name = (
                path.relative_to(transform_plugin).as_posix()
                if transform_plugin.is_dir()
                else path.name
            )
            hashes.append((logical_name, hashlib.sha256(path.read_bytes()).hexdigest()))
    revision = hashlib.sha256(canonical_json(hashes).encode()).hexdigest()[:16]
    return ExtractionProfile(
        application_version=application_version,
        extractor_version="evidence-extractor-v2",
        arelle_version=arelle_version,
        validation_options=(
            ("validate", "true"),
            ("calcs", "c10d"),
            ("validateDuplicateFacts", "all"),
            ("logLevel", "WARNING"),
            ("logTextMaxLength", "10000"),
        ),
        transform_plugin_revision=revision,
        transform_plugin_hashes=tuple(hashes),
        serialization_version="evidence-v2",
    )


def detach_filing_evidence(
    model: Any,
    *,
    company: Company,
    filing: Filing,
    raw_log_json: str,
    capture_area: Path,
    extraction_profile: ExtractionProfile,
) -> FilingEvidenceBundle:
    """Detach every Arelle-exposed filing observation before its Session closes."""
    capture_area.mkdir(parents=True, exist_ok=True)
    documents, document_objects = _capture_source_documents(model, filing, capture_area)
    document_keys = {id(value): key for key, value in document_objects.items()}
    filing_sections: tuple[FilingSectionRecord, ...] = ()
    if extraction_profile.serialization_version == "evidence-v2":
        primary_document = _primary_document(documents, filing)
        if primary_document.captured_path is None:
            raise CaptureError("The primary filing document has no captured bytes.")
        filing_sections = extract_filing_sections(
            Path(primary_document.captured_path).read_bytes(),
            form=filing.form,
            source_document_key=primary_document.key,
        )

    gathered, top_level_orders, parent_objects, origins = _gather_observations(model)
    context_objects = _gather_referenced_and_inventory(
        getattr(model, "contexts", {}).values(),
        (getattr(fact, "context", None) for fact in gathered),
    )
    unit_objects = _gather_referenced_and_inventory(
        getattr(model, "units", {}).values(),
        (getattr(fact, "unit", None) for fact in gathered),
    )
    context_keys = {
        id(context): _object_key("context", context, document_keys, index)
        for index, context in enumerate(_sort_objects(context_objects, document_keys))
    }
    unit_keys = {
        id(unit): _object_key("unit", unit, document_keys, index)
        for index, unit in enumerate(_sort_objects(unit_objects, document_keys))
    }
    contexts = tuple(
        _detach_context(context, index, context_keys[id(context)], document_keys)
        for index, context in enumerate(_sort_objects(context_objects, document_keys))
    )
    units = tuple(
        _detach_unit(unit, index, unit_keys[id(unit)], document_keys)
        for index, unit in enumerate(_sort_objects(unit_objects, document_keys))
    )

    relationship_result = _detach_calculations(model, document_keys)
    networks, relationships, relationship_concepts = relationship_result
    concept_objects: dict[str, Any] = {}
    for fact in gathered:
        _remember_concept(concept_objects, getattr(fact, "concept", None))
    for context in context_objects:
        for dimension_value in getattr(context, "qnameDims", {}).values():
            _remember_concept(concept_objects, getattr(dimension_value, "dimension", None))
            _remember_concept(concept_objects, getattr(dimension_value, "member", None))
    concept_objects.update(relationship_concepts)
    concepts = tuple(
        _detach_concept(key, concept_objects[key], document_keys) for key in sorted(concept_objects)
    )
    labels = _detach_labels(model, concept_objects, document_keys)

    ordered_facts = _sort_objects(gathered, document_keys)
    fact_keys = {
        id(fact): _object_key("fact", fact, document_keys, index)
        for index, fact in enumerate(ordered_facts)
    }
    diagnostics: list[ExtractionDiagnosticRecord] = []
    observations: list[ObservationRecord] = []
    for source_order, fact in enumerate(ordered_facts):
        observation, diagnostic = _detach_observation(
            fact,
            source_order=source_order,
            key=fact_keys[id(fact)],
            origin=origins[id(fact)],
            top_level_order=top_level_orders.get(id(fact)),
            parent_fact_key=fact_keys.get(id(parent_objects.get(id(fact)))),
            document_keys=document_keys,
            context_keys=context_keys,
            unit_keys=unit_keys,
        )
        observations.append(observation)
        if diagnostic is not None:
            diagnostics.append(diagnostic)

    validation = _detach_validation(raw_log_json, observations)
    recognized_count = sum(item.observation_origin == "recognized" for item in observations)
    unresolved_count = len(observations) - recognized_count
    coverage = CoverageManifest(
        recognized_fact_count=recognized_count,
        unresolved_observation_count=unresolved_count,
        numeric_count=sum(item.is_numeric is True for item in observations),
        nonnumeric_count=sum(item.is_numeric is False for item in observations),
        nil_count=sum(item.is_nil for item in observations),
        invalid_count=sum(
            item.validity_code is None or item.validity_code < VALID for item in observations
        ),
        context_count=len(contexts),
        unit_count=len(units),
        validation_message_count=len(validation),
        calculation_relationship_count=len(relationships),
        source_document_count=len(documents),
        filing_section_count=len(filing_sections),
        extracted_filing_section_count=sum(
            section.extraction_status == "extracted" for section in filing_sections
        ),
    )
    fiscal_year, fiscal_year_source = _fiscal_year(observations, filing)
    fiscal_period, fiscal_period_source = _fiscal_period(observations, filing)
    recognized_identities = {id(fact) for fact in gathered if origins[id(fact)] == "recognized"}
    if recognized_count != len(recognized_identities):
        raise ExtractionError("Recognized observation traversal count changed during detachment.")
    return FilingEvidenceBundle(
        company=company,
        filing=filing,
        captured_company_name=company.name,
        captured_company_ticker=company.ticker,
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        fiscal_year_source=fiscal_year_source,
        fiscal_period_source=fiscal_period_source,
        source_documents=documents,
        concepts=concepts,
        concept_labels=labels,
        contexts=contexts,
        units=units,
        observations=tuple(observations),
        diagnostics=tuple(diagnostics),
        validation_messages=validation,
        calculation_networks=networks,
        calculation_relationships=relationships,
        extraction_profile=extraction_profile,
        coverage_manifest=coverage,
        raw_log_json=raw_log_json,
        filing_sections=filing_sections,
    )


def _capture_source_documents(
    model: Any, filing: Filing, capture_area: Path
) -> tuple[tuple[SourceDocumentRecord, ...], dict[str, Any]]:
    url_documents = getattr(model, "urlDocs", {})
    if not isinstance(url_documents, dict) or not url_documents:
        raise CaptureError("Arelle did not expose loaded source documents.")
    filing_root = filing.url.rsplit("/", maxsplit=1)[0] + "/"
    records: list[SourceDocumentRecord] = []
    objects: dict[str, Any] = {}
    sorted_documents = sorted(url_documents.items(), key=lambda item: str(item[0]))
    parent_references: dict[int, list[tuple[str, str]]] = {}
    for _parent_uri_value, parent in sorted_documents:
        parent_uri = str(getattr(parent, "uri", _parent_uri_value))
        for referenced, reference in getattr(parent, "referencesDocument", {}).items():
            parent_references.setdefault(id(referenced), []).append(
                (parent_uri, _document_reference_text(reference))
            )
    for index, (uri_value, document) in enumerate(sorted_documents):
        uri = str(getattr(document, "uri", uri_value))
        document_type = int(getattr(document, "type", -1))
        kind = (
            Type.typeName[document_type]
            if 0 <= document_type < len(Type.typeName)
            else f"unknown-{document_type}"
        )
        generated = (
            document_type in {Type.INSTANCE, Type.INLINEXBRLDOCUMENTSET} and uri != filing.url
        )
        filing_owned = uri.startswith(filing_root) or uri == filing.url
        should_retain = document_type == Type.INLINEXBRL or (
            filing_owned and document_type in {Type.SCHEMA, Type.LINKBASE, Type.INSTANCE}
        )
        if generated:
            retention_kind = "generated_document"
        elif should_retain:
            retention_kind = "retained_original"
        else:
            retention_kind = "external_dependency_reference"
        key = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        objects[key] = document
        captured_path: str | None = None
        content_hash: str | None = None
        byte_size: int | None = None
        if retention_kind == "retained_original":
            source = Path(str(getattr(document, "filepath", "")))
            if not source.is_file():
                raise CaptureError(f"Required loaded source file is unavailable for {uri}.")
            suffix = source.suffix.lower() or ".bin"
            destination = capture_area / f"source-{index:04d}-{key[:16]}{suffix}"
            try:
                with source.open("rb") as input_stream, destination.open("xb") as output_stream:
                    shutil.copyfileobj(input_stream, output_stream)
                    output_stream.flush()
                    os.fsync(output_stream.fileno())
            except FileExistsError:
                pass
            except OSError as exc:
                raise CaptureError(f"Could not capture source file {uri}: {exc}") from exc
            raw = destination.read_bytes()
            content_hash = hashlib.sha256(raw).hexdigest()
            byte_size = len(raw)
            captured_path = str(destination)
        records.append(
            SourceDocumentRecord(
                key=key,
                original_uri=uri,
                document_kind=kind,
                retention_kind=retention_kind,  # type: ignore[arg-type]
                captured_path=captured_path,
                content_hash=content_hash,
                byte_size=byte_size,
                media_type=_media_type(uri),
                parent_uri=(
                    sorted(parent_references[id(document)])[0][0]
                    if id(document) in parent_references
                    else None
                ),
                source_reference=(
                    sorted(parent_references[id(document)])[0][1]
                    if id(document) in parent_references
                    else None
                ),
            )
        )
    if not any(record.original_uri == filing.url and record.content_hash for record in records):
        primary_name = filing.primary_document.casefold()
        if not any(
            record.original_uri.casefold().endswith(primary_name) and record.content_hash
            for record in records
        ):
            raise CaptureError("The exact primary Inline XBRL document was not captured.")
    return tuple(records), objects


def _primary_document(
    documents: tuple[SourceDocumentRecord, ...], filing: Filing
) -> SourceDocumentRecord:
    exact = next(
        (
            document
            for document in documents
            if document.original_uri == filing.url and document.content_hash is not None
        ),
        None,
    )
    if exact is not None:
        return exact
    primary_name = filing.primary_document.casefold()
    fallback = next(
        (
            document
            for document in documents
            if document.original_uri.casefold().endswith(primary_name)
            and document.content_hash is not None
        ),
        None,
    )
    if fallback is None:
        raise CaptureError("The exact primary filing document was not captured.")
    return fallback


def _gather_observations(
    model: Any,
) -> tuple[list[Any], dict[int, int], dict[int, Any], dict[int, str]]:
    gathered: list[Any] = []
    seen: set[int] = set()
    top_level_orders: dict[int, int] = {}
    parents: dict[int, Any] = {}
    origins: dict[int, str] = {}

    def visit(fact: Any, *, parent: Any | None, top_level_order: int | None) -> None:
        identity = id(fact)
        if identity in seen:
            return
        seen.add(identity)
        gathered.append(fact)
        origins[identity] = "recognized"
        if parent is not None:
            parents[identity] = parent
        if top_level_order is not None:
            top_level_orders[identity] = top_level_order
        for child in getattr(fact, "modelTupleFacts", ()) or ():
            visit(child, parent=fact, top_level_order=None)

    for index, fact in enumerate(getattr(model, "facts", ()) or ()):
        visit(fact, parent=None, top_level_order=index)
    for fact in _sort_objects(tuple(getattr(model, "factsInInstance", ()) or ()), {}):
        visit(fact, parent=None, top_level_order=None)
    for fact in _sort_objects(tuple(getattr(model, "undefinedFacts", ()) or ()), {}):
        identity = id(fact)
        if identity not in seen:
            seen.add(identity)
            gathered.append(fact)
        origins[identity] = "undefined"
    return gathered, top_level_orders, parents, origins


def _gather_referenced_and_inventory(inventory: Any, referenced: Any) -> list[Any]:
    result: list[Any] = []
    seen: set[int] = set()
    for item in (*tuple(inventory), *tuple(referenced)):
        if item is None or id(item) in seen:
            continue
        seen.add(id(item))
        result.append(item)
    return result


def _sort_objects(values: Any, document_keys: dict[int, str]) -> list[Any]:
    return sorted(
        tuple(values),
        key=lambda value: (
            document_keys.get(id(getattr(value, "modelDocument", None)), ""),
            _node_path(value),
            int(getattr(value, "sourceline", 0) or 0),
            str(getattr(value, "qname", "")),
            str(getattr(value, "value", "")),
        ),
    )


def _object_key(kind: str, value: Any, document_keys: dict[int, str], fallback_order: int) -> str:
    document_key = document_keys.get(id(getattr(value, "modelDocument", None)), "no-document")
    material = f"{kind}|{document_key}|{_node_path(value)}|{fallback_order}"
    return f"{kind}:{hashlib.sha256(material.encode()).hexdigest()}"


def _node_path(value: Any) -> str:
    node = getattr(value, "arcElement", value)
    try:
        tree = node.getroottree()
        return str(tree.getpath(node))
    except (AttributeError, TypeError, ValueError):
        object_index = getattr(value, "objectIndex", None)
        xml_id = getattr(value, "id", None)
        return f"object[{object_index if object_index is not None else xml_id or 'unknown'}]"


def _source_document_key(value: Any, document_keys: dict[int, str]) -> str | None:
    return document_keys.get(id(getattr(value, "modelDocument", None)))


def _expanded_name(value: Any) -> tuple[str, str, str]:
    if value is None:
        return "", "", ""
    namespace = str(getattr(value, "namespaceURI", "") or "")
    local_name = str(getattr(value, "localName", "") or "")
    display = str(value)
    if not local_name:
        if display.startswith("{") and "}" in display:
            namespace, local_name = display[1:].split("}", maxsplit=1)
        else:
            _prefix, separator, possible_local = display.rpartition(":")
            local_name = possible_local if separator else display
    return namespace, local_name, display


def _concept_key(value: Any) -> str | None:
    qname = getattr(value, "qname", None)
    namespace, local_name, _display = _expanded_name(qname)
    return f"{{{namespace}}}{local_name}" if local_name else None


def _remember_concept(destination: dict[str, Any], concept: Any) -> None:
    key = _concept_key(concept)
    if key is not None:
        destination[key] = concept


def _detach_concept(key: str, concept: Any, document_keys: dict[int, str]) -> ConceptRecord:
    namespace, local_name, display = _expanded_name(getattr(concept, "qname", None))
    data_type = getattr(getattr(concept, "type", None), "qname", None)
    type_namespace, type_local_name, _type_display = _expanded_name(data_type)
    return ConceptRecord(
        key=key,
        namespace_uri=namespace,
        local_name=local_name,
        display_qname=display,
        definition_status="resolved",
        data_type_namespace_uri=type_namespace or None,
        data_type_local_name=type_local_name or None,
        period_type=_optional_text(getattr(concept, "periodType", None)),
        balance=_optional_text(getattr(concept, "balance", None)),
        is_numeric=_optional_bool(getattr(concept, "isNumeric", None)),
        is_abstract=_optional_bool(getattr(concept, "isAbstract", None)),
        source_document_key=_source_document_key(concept, document_keys),
        source_locator=_node_path(concept),
    )


def _detach_labels(
    model: Any, concepts: dict[str, Any], document_keys: dict[int, str]
) -> tuple[ConceptLabelRecord, ...]:
    labels: list[ConceptLabelRecord] = []
    relationship_set = model.relationshipSet(XbrlConst.conceptLabel)
    relationships = getattr(relationship_set, "modelRelationships", ()) or ()
    for relationship in relationships:
        concept = getattr(relationship, "fromModelObject", None)
        resource = getattr(relationship, "toModelObject", None)
        key = _concept_key(concept)
        if key not in concepts or resource is None:
            continue
        labels.append(
            ConceptLabelRecord(
                concept_key=key,
                role_uri=str(getattr(resource, "role", "") or ""),
                language=str(getattr(resource, "xmlLang", "") or ""),
                label_text=str(
                    getattr(resource, "textValue", None)
                    or getattr(resource, "stringValue", None)
                    or ""
                ),
                source_order=len(labels),
                source_document_key=_source_document_key(resource, document_keys),
                source_locator=_node_path(resource),
            )
        )
    if labels:
        return tuple(labels)
    for key in sorted(concepts):
        concept = concepts[key]
        try:
            label = str(concept.label(fallbackToQname=True, lang="en", strip=True))
        except (AttributeError, TypeError, ValueError):
            label = str(getattr(concept, "qname", key))
        labels.append(
            ConceptLabelRecord(
                concept_key=key,
                role_uri=XbrlConst.standardLabel,
                language="en",
                label_text=label,
                source_order=len(labels),
            )
        )
    return tuple(labels)


def _detach_context(
    context: Any,
    source_order: int,
    key: str,
    document_keys: dict[int, str],
) -> ContextRecord:
    if getattr(context, "isInstantPeriod", False):
        period_kind: Literal["instant", "duration", "forever", "unknown"] = "instant"
    elif getattr(context, "isStartEndPeriod", False):
        period_kind = "duration"
    elif getattr(context, "isForeverPeriod", False):
        period_kind = "forever"
    else:
        period_kind = "unknown"
    start = getattr(context, "startDatetime", None)
    exclusive_end = getattr(context, "endDatetime", None)
    report_end = (
        (exclusive_end - timedelta(days=1)).date() if isinstance(exclusive_end, datetime) else None
    )
    entity_identifier = getattr(context, "entityIdentifier", None)
    if not isinstance(entity_identifier, tuple) or len(entity_identifier) != 2:
        entity_identifier = (None, None)
    dimensions: list[ContextDimensionRecord] = []
    for index, (axis_qname, value) in enumerate(
        sorted(
            getattr(context, "qnameDims", {}).items(),
            key=lambda item: str(item[0]).casefold(),
        )
    ):
        axis_namespace, axis_local_name, _axis_display = _expanded_name(axis_qname)
        member_qname = getattr(value, "memberQname", None)
        member_namespace, member_local_name, _member_display = _expanded_name(member_qname)
        typed_member = getattr(value, "typedMember", None)
        if member_qname is not None:
            member_kind = "explicit"
            typed_xml = None
            typed_text = None
            typed_hash = None
        elif typed_member is not None:
            member_kind = "typed"
            typed_xml = _xml(typed_member)
            typed_text = (
                "".join(typed_member.itertext())
                if hasattr(typed_member, "itertext")
                else str(typed_member)
            )
            typed_hash = hashlib.sha256(typed_xml.encode("utf-8")).hexdigest()
            member_namespace = ""
            member_local_name = ""
        else:
            member_kind = "unresolved"
            typed_xml = None
            typed_text = None
            typed_hash = None
            member_namespace = ""
            member_local_name = ""
        dimensions.append(
            ContextDimensionRecord(
                source_order=index,
                axis_namespace_uri=axis_namespace,
                axis_local_name=axis_local_name,
                member_kind=member_kind,  # type: ignore[arg-type]
                context_element=str(getattr(value, "contextElement", "unknown")),
                axis_concept_key=_concept_key(getattr(value, "dimension", None)),
                member_concept_key=_concept_key(getattr(value, "member", None)),
                explicit_member_namespace_uri=member_namespace or None,
                explicit_member_local_name=member_local_name or None,
                typed_xml=typed_xml,
                typed_text=typed_text,
                typed_hash=typed_hash,
            )
        )
    raw_xml = _xml(context)
    instant_datetime = getattr(context, "instantDatetime", None)
    return ContextRecord(
        key=key,
        source_order=source_order,
        source_document_key=_source_document_key(context, document_keys),
        source_locator=_node_path(context),
        raw_xml=raw_xml,
        period_kind=period_kind,
        xml_id=_optional_text(getattr(context, "id", None)),
        entity_scheme=_optional_text(entity_identifier[0]),
        entity_identifier=_optional_text(entity_identifier[1]),
        raw_start=start.isoformat() if isinstance(start, datetime) else None,
        raw_end=exclusive_end.isoformat() if isinstance(exclusive_end, datetime) else None,
        raw_instant=(
            instant_datetime.isoformat() if isinstance(instant_datetime, datetime) else None
        ),
        period_start_date=start.date() if isinstance(start, datetime) else None,
        period_end_date=report_end,
        exclusive_end=exclusive_end.isoformat() if isinstance(exclusive_end, datetime) else None,
        segment_xml=_xml_or_none(getattr(context, "segment", None)),
        scenario_xml=_xml_or_none(getattr(context, "scenario", None)),
        canonical_hash=hashlib.sha256(raw_xml.encode("utf-8")).hexdigest(),
        legacy_report_dimensions=_legacy_dimension_texts(context),
        dimensions=tuple(dimensions),
    )


def _detach_unit(
    unit: Any,
    source_order: int,
    key: str,
    document_keys: dict[int, str],
) -> UnitRecord:
    measures: list[UnitMeasureRecord] = []
    numerator, denominator = getattr(unit, "measures", ((), ()))
    for side, values in (("numerator", numerator), ("denominator", denominator)):
        for index, measure in enumerate(values):
            namespace, local_name, display = _expanded_name(measure)
            measures.append(
                UnitMeasureRecord(
                    side=side,  # type: ignore[arg-type]
                    measure_order=index,
                    namespace_uri=namespace,
                    local_name=local_name,
                    display_qname=display,
                )
            )
    raw_xml = _xml(unit)
    return UnitRecord(
        key=key,
        source_order=source_order,
        source_document_key=_source_document_key(unit, document_keys),
        source_locator=_node_path(unit),
        raw_xml=raw_xml,
        xml_id=_optional_text(getattr(unit, "id", None)),
        legacy_report_unit_text=_legacy_unit_text(unit),
        canonical_hash=hashlib.sha256(raw_xml.encode("utf-8")).hexdigest(),
        measures=tuple(measures),
    )


def _detach_observation(
    fact: Any,
    *,
    source_order: int,
    key: str,
    origin: str,
    top_level_order: int | None,
    parent_fact_key: str | None,
    document_keys: dict[int, str],
    context_keys: dict[int, str],
    unit_keys: dict[int, str],
) -> tuple[ObservationRecord, ExtractionDiagnosticRecord | None]:
    concept = getattr(fact, "concept", None)
    display_qname = _optional_text(getattr(fact, "qname", None))
    context = getattr(fact, "context", None)
    unit = getattr(fact, "unit", None)
    is_numeric = _optional_bool(getattr(fact, "isNumeric", None))
    is_nil = bool(getattr(fact, "isNil", False))
    validity = getattr(fact, "xValid", None)
    validity_code = int(validity) if isinstance(validity, int) else None
    typed_kind: str | None = None
    typed_text: str | None = None
    conversion_error: str | None = None
    if not is_nil:
        typed_kind, typed_text, conversion_error = _serialize_typed_value(
            getattr(fact, "xValue", None),
            source_value=_optional_text(getattr(fact, "value", None)),
        )
    label: str | None = None
    if concept is not None:
        try:
            label = str(concept.label(fallbackToQname=True, lang="en", strip=True))
        except (AttributeError, TypeError, ValueError):
            label = display_qname
    inline_metadata = {
        name: value for name in _INLINE_ATTRIBUTES if (value := _attribute(fact, name)) is not None
    }
    fact_kind: Literal["item", "tuple", "unresolved"] = (
        "unresolved"
        if origin == "undefined" or concept is None
        else "tuple"
        if bool(getattr(fact, "isTuple", False))
        else "item"
    )
    observation = ObservationRecord(
        key=key,
        source_order=source_order,
        observation_origin=origin,  # type: ignore[arg-type]
        fact_kind=fact_kind,
        source_document_key=_source_document_key(fact, document_keys),
        source_locator=_node_path(fact),
        is_nil=is_nil,
        validity_code=validity_code,
        validity_name=_VALIDITY_NAMES.get(validity_code, f"code-{validity_code}"),
        concept_key=_concept_key(concept),
        display_qname=display_qname,
        context_key=context_keys.get(id(context)),
        unit_key=unit_keys.get(id(unit)),
        raw_context_ref=_optional_text(
            getattr(fact, "contextID", None) or _attribute(fact, "contextRef")
        ),
        raw_unit_ref=_optional_text(getattr(fact, "unitID", None) or _attribute(fact, "unitRef")),
        parent_fact_key=parent_fact_key,
        xml_id=_optional_text(getattr(fact, "id", None) or _attribute(fact, "id")),
        source_line=(
            int(getattr(fact, "sourceline", 0)) if getattr(fact, "sourceline", None) else None
        ),
        top_level_order=top_level_order,
        raw_value_text=_optional_text(getattr(fact, "value", None)),
        typed_value_kind=typed_kind,
        typed_value_text=typed_text,
        is_numeric=is_numeric,
        numeric_conversion_error=conversion_error,
        decimals=_optional_text(getattr(fact, "decimals", None) or _attribute(fact, "decimals")),
        precision=_optional_text(getattr(fact, "precision", None) or _attribute(fact, "precision")),
        language=_optional_text(getattr(fact, "xmlLang", None)),
        inline_metadata_json=canonical_json(inline_metadata) if inline_metadata else None,
        legacy_report_label_text=label,
    )
    diagnostic = None
    if conversion_error is not None:
        diagnostic = ExtractionDiagnosticRecord(
            code="TYPED_VALUE_SERIALIZATION_FAILED",
            severity="error",
            message=conversion_error,
            source_order=source_order,
            fact_key=key,
        )
    return observation, diagnostic


def _serialize_typed_value(
    value: Any, *, source_value: str | None
) -> tuple[str | None, str | None, str | None]:
    if value is None:
        return None, None, None
    if isinstance(value, bool):
        return "boolean", "true" if value else "false", None
    if isinstance(value, Decimal):
        return "decimal", str(value), None
    if isinstance(value, int):
        return "integer", str(value), None
    if isinstance(value, float):
        return "float", repr(value), None
    if isinstance(value, datetime):
        return "datetime", value.isoformat(), None
    if isinstance(value, date):
        return "date", value.isoformat(), None
    if isinstance(value, str):
        return "string", value, None
    if hasattr(value, "namespaceURI") and hasattr(value, "localName"):
        namespace, local_name, _display = _expanded_name(value)
        return "qname", f"{{{namespace}}}{local_name}", None
    try:
        if isinstance(value, etree._Element):
            return "xml", _xml(value), None
    except TypeError:
        pass
    return (
        "unsupported",
        source_value,
        f"Unsupported typed value {type(value).__module__}.{type(value).__qualname__}",
    )


def _detach_validation(
    raw_log_json: str, observations: list[ObservationRecord]
) -> tuple[ValidationRecord, ...]:
    try:
        payload = json.loads(raw_log_json)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ExtractionError("Arelle returned an invalid JSON validation log.") from exc
    records = payload.get("log", []) if isinstance(payload, dict) else []
    if not isinstance(records, list):
        raise ExtractionError("Arelle validation log does not contain an ordered log list.")
    by_locator: dict[str, list[str]] = {}
    for observation in observations:
        by_locator.setdefault(observation.source_locator, []).append(observation.key)
    detached: list[ValidationRecord] = []
    for message_order, record in enumerate(records):
        if not isinstance(record, dict):
            raise ExtractionError("Arelle validation log contains a non-record item.")
        message_value = record.get("message", "")
        if isinstance(message_value, dict):
            message_text = str(message_value.get("text", ""))
        else:
            message_text = str(message_value)
        raw_references = record.get("refs", record.get("references", []))
        if isinstance(raw_references, dict):
            raw_references = [raw_references]
        if not isinstance(raw_references, list):
            raw_references = [raw_references]
        references: list[ValidationReferenceRecord] = []
        for reference_order, reference in enumerate(raw_references):
            raw_reference_json = canonical_json(reference)
            locator = ""
            if isinstance(reference, dict):
                locator = str(reference.get("path", reference.get("sourceLine", "")))
            matches = by_locator.get(locator, [])
            resolution_status: Literal["resolved", "ambiguous", "unresolved"] = (
                "resolved"
                if len(matches) == 1
                else "ambiguous"
                if len(matches) > 1
                else "unresolved"
            )
            references.append(
                ValidationReferenceRecord(
                    reference_order=reference_order,
                    raw_reference_json=raw_reference_json,
                    resolution_status=resolution_status,
                    fact_key=matches[0] if len(matches) == 1 else None,
                    source_locator=locator or None,
                )
            )
        detached.append(
            ValidationRecord(
                message_order=message_order,
                level=str(record.get("level", "unknown")),
                code=str(record.get("code", "arelle")),
                message_text=" ".join(message_text.split()),
                raw_record_json=canonical_json(record),
                references=tuple(references),
            )
        )
    return tuple(detached)


def _detach_calculations(
    model: Any, document_keys: dict[int, str]
) -> tuple[
    tuple[CalculationNetworkRecord, ...],
    tuple[CalculationRelationshipRecord, ...],
    dict[str, Any],
]:
    networks: list[CalculationNetworkRecord] = []
    relationships: list[CalculationRelationshipRecord] = []
    concepts: dict[str, Any] = {}
    for arcrole in XbrlConst.summationItems:
        try:
            relationship_set = model.relationshipSet(arcrole)
            model_relationships = tuple(getattr(relationship_set, "modelRelationships", ()) or ())
        except Exception as exc:
            raise ExtractionError(
                f"Could not extract calculation network {arcrole}: {exc}"
            ) from exc
        networks.append(
            CalculationNetworkRecord(
                arcrole_uri=arcrole,
                extraction_status=("extracted" if model_relationships else "extracted_empty"),
                relationship_count=len(model_relationships),
            )
        )
        ordered = sorted(
            model_relationships,
            key=lambda item: (
                str(getattr(item, "linkrole", "")),
                _node_path(item),
                str(getattr(item, "order", "")),
                str(getattr(getattr(item, "fromModelObject", None), "qname", "")),
                str(getattr(getattr(item, "toModelObject", None), "qname", "")),
            ),
        )
        for index, relationship in enumerate(ordered):
            parent = getattr(relationship, "fromModelObject", None)
            child = getattr(relationship, "toModelObject", None)
            parent_key = _concept_key(parent)
            child_key = _concept_key(child)
            if parent_key is None or child_key is None:
                raise ExtractionError("Calculation relationship has an unresolved endpoint.")
            concepts[parent_key] = parent
            concepts[child_key] = child
            link_qname = getattr(getattr(relationship, "modelLink", None), "qname", None)
            arc_qname = getattr(relationship, "qname", None)
            link_namespace, link_local_name, _link_display = _expanded_name(link_qname)
            arc_namespace, arc_local_name, _arc_display = _expanded_name(arc_qname)
            role_uri = str(getattr(relationship, "linkrole", "") or "")
            weight = getattr(relationship, "weight", None)
            if weight is None:
                raise ExtractionError("Calculation relationship has no exact weight.")
            try:
                role_definition = str(model.roleTypeDefinition(role_uri))
            except (AttributeError, TypeError, ValueError):
                role_definition = None
            relationships.append(
                CalculationRelationshipRecord(
                    arcrole_uri=arcrole,
                    role_uri=role_uri,
                    relationship_order=index,
                    parent_concept_key=parent_key,
                    child_concept_key=child_key,
                    exact_weight_text=str(weight),
                    exact_order_text=_optional_text(getattr(relationship, "order", None)),
                    role_definition=role_definition,
                    link_namespace_uri=link_namespace or None,
                    link_local_name=link_local_name or None,
                    arc_namespace_uri=arc_namespace or None,
                    arc_local_name=arc_local_name or None,
                    source_document_key=_source_document_key(relationship, document_keys),
                    source_locator=_node_path(relationship),
                )
            )
    return tuple(networks), tuple(relationships), concepts


def _fiscal_year(observations: list[ObservationRecord], filing: Filing) -> tuple[int, str]:
    for observation in observations:
        if observation.display_qname != "dei:DocumentFiscalYearFocus":
            continue
        try:
            return int(observation.typed_value_text or ""), observation.key
        except ValueError:
            continue
    return filing.report_date.year, "filing-report-date"


def _fiscal_period(observations: list[ObservationRecord], filing: Filing) -> tuple[str, str]:
    for observation in observations:
        if observation.display_qname != "dei:DocumentFiscalPeriodFocus":
            continue
        fiscal_period = (observation.typed_value_text or "").upper()
        if fiscal_period in {"FY", "Q1", "Q2", "Q3"}:
            return fiscal_period, observation.key
    return f"ended-{filing.report_date.isoformat()}", "filing-report-date"


def _legacy_unit_text(unit: Any) -> str:
    numerator, denominator = getattr(unit, "measures", ((), ()))
    numerator_text = "*".join(str(measure) for measure in numerator) or "1"
    denominator_text = "*".join(str(measure) for measure in denominator)
    return f"{numerator_text}/{denominator_text}" if denominator_text else numerator_text


def _legacy_dimension_texts(context: Any) -> tuple[str, ...]:
    dimensions: list[str] = []
    for dimension_qname, value in sorted(
        getattr(context, "qnameDims", {}).items(), key=lambda item: str(item[0]).casefold()
    ):
        member_qname = getattr(value, "memberQname", None)
        if member_qname is not None:
            dimensions.append(f"{dimension_qname}={member_qname}")
            continue
        typed_member = getattr(value, "typedMember", None)
        dimensions.append(f"{dimension_qname}={typed_member if typed_member is not None else '?'}")
    return tuple(dimensions)


def _xml(value: Any) -> str:
    try:
        return str(etree.tostring(value, encoding="unicode", with_tail=False))
    except (TypeError, ValueError):
        return ""


def _xml_or_none(value: Any) -> str | None:
    return _xml(value) if value is not None else None


def _attribute(value: Any, name: str) -> str | None:
    try:
        result = value.get(name)
    except (AttributeError, TypeError, ValueError):
        result = getattr(value, name, None)
    return _optional_text(result)


def _optional_text(value: Any) -> str | None:
    return None if value is None else str(value)


def _optional_bool(value: Any) -> bool | None:
    return None if value is None else bool(value)


def _media_type(uri: str) -> str:
    suffix = Path(uri.split("?", maxsplit=1)[0]).suffix.lower()
    return {
        ".htm": "text/html",
        ".html": "text/html",
        ".xhtml": "application/xhtml+xml",
        ".xml": "application/xml",
        ".xsd": "application/xml",
    }.get(suffix, "application/octet-stream")


def _document_reference_text(reference: Any) -> str:
    referring_object = getattr(reference, "referringModelObject", None)
    return canonical_json(
        {
            "reference_types": sorted(
                str(value) for value in getattr(reference, "referenceTypes", ())
            ),
            "referring_locator": (
                _node_path(referring_object) if referring_object is not None else None
            ),
            "referring_xlink_role": _optional_text(getattr(reference, "referringXlinkRole", None)),
        }
    )
