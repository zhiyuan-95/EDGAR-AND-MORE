from __future__ import annotations

from datetime import date

from arelle.XmlValidateConst import VALID

from sec_inline_financials.company_lineage import RegistrantIdentity, ReportingTransition
from sec_inline_financials.evidence_classification import REPORT_RULE_VERSION, classify_report
from sec_inline_financials.evidence_models import (
    ContextDimensionRecord,
    ContextRecord,
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
    ObservationRecord,
    UnitRecord,
)
from sec_inline_financials.models import Company, Filing

_SUCCESSOR = RegistrantIdentity("0000000002", "Successor Corp")
_PREDECESSOR = RegistrantIdentity("0000000001", "Predecessor Corp")
_TRANSITION = ReportingTransition(
    predecessor=_PREDECESSOR,
    successor=_SUCCESSOR,
    effective_date=date(2026, 4, 1),
)


def test_report_v2_removes_verified_predecessor_legal_entity_axis_wrapper() -> None:
    bundle = _bundle(
        _context(
            entity_identifier=_SUCCESSOR.cik,
            dimensions=(_legal_entity_axis("Predecessor Corp Member"),),
        )
    )

    evaluation = classify_report(
        bundle,
        "quarterly",
        REPORT_RULE_VERSION,
        transition=_TRANSITION,
    )

    assert len(evaluation.statuses) == 1
    status = evaluation.statuses[0]
    assert status.evidence_role == "selected_primary"
    assert status.selection_note is not None
    assert "removed verified transition LegalEntityAxis wrapper" in status.selection_note


def test_report_v2_excludes_pre_transition_successor_context_without_verified_predecessor() -> None:
    bundle = _bundle(
        _context(
            entity_identifier=_SUCCESSOR.cik,
            dimensions=(_legal_entity_axis("Unrelated Corp Member"),),
        )
    )

    evaluation = classify_report(
        bundle,
        "quarterly",
        REPORT_RULE_VERSION,
        transition=_TRANSITION,
    )

    assert len(evaluation.statuses) == 1
    status = evaluation.statuses[0]
    assert status.evidence_role == "excluded"
    assert tuple(reason[0] for reason in status.exclusion_reasons) == (
        "TRANSITION_NON_REPORTING_ENTITY",
    )


def _bundle(context: ContextRecord) -> FilingEvidenceBundle:
    report_date = date(2026, 3, 31)
    return FilingEvidenceBundle(
        company=Company(ticker="NEW", cik=_SUCCESSOR.cik, name=_SUCCESSOR.legal_name),
        filing=Filing(
            accession="0000000002-26-000001",
            filing_date=date(2026, 5, 1),
            report_date=report_date,
            form="10-Q",
            primary_document="new-20260331.htm",
            url="https://www.sec.gov/Archives/edgar/data/2/000000000226000001/new-20260331.htm",
            registrant_cik=_SUCCESSOR.cik,
            archive_owner_cik=_SUCCESSOR.cik,
        ),
        captured_company_name=_SUCCESSOR.legal_name,
        captured_company_ticker="NEW",
        fiscal_year=2026,
        fiscal_period="Q1",
        fiscal_year_source="document",
        fiscal_period_source="document",
        source_documents=(),
        concepts=(),
        concept_labels=(),
        contexts=(context,),
        units=(
            UnitRecord(
                key="u1",
                source_order=0,
                source_document_key=None,
                source_locator="/xbrl/unit[1]",
                raw_xml="<unit id='u1'/>",
                legacy_report_unit_text="USD",
            ),
        ),
        observations=(
            ObservationRecord(
                key="f1",
                source_order=0,
                observation_origin="recognized",
                fact_kind="item",
                source_document_key=None,
                source_locator="/xbrl/fact[1]",
                is_nil=False,
                validity_code=VALID,
                validity_name="VALID",
                concept_key="us-gaap:Revenues",
                display_qname="us-gaap:Revenues",
                context_key=context.key,
                unit_key="u1",
                raw_context_ref=context.xml_id,
                raw_unit_ref="u1",
                top_level_order=0,
                raw_value_text="100",
                typed_value_kind="decimal",
                typed_value_text="100",
                is_numeric=True,
                decimals="0",
            ),
        ),
        diagnostics=(),
        validation_messages=(),
        calculation_networks=(),
        calculation_relationships=(),
        extraction_profile=ExtractionProfile(
            application_version="test",
            extractor_version="test",
            arelle_version="test",
            validation_options=(),
            transform_plugin_revision="test",
            transform_plugin_hashes=(),
        ),
        coverage_manifest=CoverageManifest(
            recognized_fact_count=1,
            unresolved_observation_count=0,
            numeric_count=1,
            nonnumeric_count=0,
            nil_count=0,
            invalid_count=0,
            context_count=1,
            unit_count=1,
            validation_message_count=0,
            calculation_relationship_count=0,
            source_document_count=0,
        ),
        raw_log_json="{}",
    )


def _context(
    *,
    entity_identifier: str,
    dimensions: tuple[ContextDimensionRecord, ...],
) -> ContextRecord:
    return ContextRecord(
        key="c1",
        source_order=0,
        source_document_key=None,
        source_locator="/xbrl/context[1]",
        raw_xml="<context id='c1'/>",
        period_kind="duration",
        xml_id="c1",
        entity_identifier=entity_identifier,
        period_start_date=date(2026, 1, 1),
        period_end_date=date(2026, 3, 31),
        dimensions=dimensions,
    )


def _legal_entity_axis(member_name: str) -> ContextDimensionRecord:
    return ContextDimensionRecord(
        source_order=0,
        axis_namespace_uri="https://xbrl.sec.gov/dei/2024",
        axis_local_name="LegalEntityAxis",
        member_kind="explicit",
        context_element="segment",
        explicit_member_namespace_uri="https://xbrl.sec.gov/dei/2024",
        explicit_member_local_name=member_name,
    )
