from datetime import date

from arelle.XmlValidateConst import VALID

from sec_inline_financials.company_lineage import RegistrantIdentity, ReportingTransition
from sec_inline_financials.evidence_classification import classify_report
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


def _dimension(axis: str, member: str) -> ContextDimensionRecord:
    return ContextDimensionRecord(
        source_order=0,
        axis_namespace_uri=(
            "http://xbrl.sec.gov/dei/2024"
            if axis == "LegalEntityAxis"
            else "http://fasb.org/us-gaap/2024"
        ),
        axis_local_name=axis,
        member_kind="explicit",
        context_element="segment",
        explicit_member_namespace_uri="https://example.com/taxonomy/2024",
        explicit_member_local_name=member,
    )


def _context(key: str, dimensions: tuple[ContextDimensionRecord, ...]) -> ContextRecord:
    return ContextRecord(
        key=key,
        source_order=len(dimensions),
        source_document_key=None,
        source_locator=f"context:{key}",
        raw_xml="<context/>",
        period_kind="instant",
        xml_id=key,
        entity_scheme="http://www.sec.gov/CIK",
        entity_identifier="0000000002",
        period_end_date=date(2024, 6, 30),
        dimensions=dimensions,
    )


def _observation(key: str, context_key: str, value: str, order: int) -> ObservationRecord:
    return ObservationRecord(
        key=key,
        source_order=order,
        observation_origin="recognized",
        fact_kind="item",
        source_document_key=None,
        source_locator=f"fact:{key}",
        is_nil=False,
        validity_code=VALID,
        validity_name="valid",
        concept_key="{http://fasb.org/us-gaap/2024}Assets",
        display_qname="us-gaap:Assets",
        context_key=context_key,
        unit_key="usd",
        top_level_order=order,
        raw_value_text=value,
        typed_value_kind="decimal",
        typed_value_text=value,
        is_numeric=True,
        decimals="-6",
    )


def _bundle() -> FilingEvidenceBundle:
    predecessor = _dimension(
        "LegalEntityAxis",
        "PredecessorLegalNameMember",
    )
    segment = _dimension("StatementBusinessSegmentsAxis", "OperatingSegmentMember")
    contexts = (
        _context("successor-shell", ()),
        _context("predecessor-total", (predecessor,)),
        _context("predecessor-segment", (predecessor, segment)),
    )
    observations = (
        _observation("shell-assets", "successor-shell", "100", 0),
        _observation("predecessor-assets", "predecessor-total", "2670900000", 1),
        _observation("segment-assets", "predecessor-segment", "900000000", 2),
    )
    return FilingEvidenceBundle(
        company=Company(ticker="NEW", cik="0000000002", name="Successor Legal Name"),
        filing=Filing(
            accession="0000000002-24-000011",
            filing_date=date(2024, 8, 9),
            report_date=date(2024, 6, 30),
            form="10-Q",
            primary_document="quarter.htm",
            url="https://www.sec.gov/Archives/example/quarter.htm",
            registrant_cik="0000000002",
            archive_owner_cik="0000000002",
        ),
        captured_company_name="Successor Legal Name",
        captured_company_ticker="NEW",
        fiscal_year=2024,
        fiscal_period="Q2",
        fiscal_year_source="test",
        fiscal_period_source="test",
        source_documents=(),
        concepts=(),
        concept_labels=(),
        contexts=contexts,
        units=(
            UnitRecord(
                key="usd",
                source_order=0,
                source_document_key=None,
                source_locator="unit:usd",
                raw_xml="<unit/>",
                legacy_report_unit_text="iso4217:USD",
            ),
        ),
        observations=observations,
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
        coverage_manifest=CoverageManifest(3, 0, 3, 0, 0, 0, 3, 1, 0, 0, 0),
        raw_log_json="[]",
    )


def _transition() -> ReportingTransition:
    return ReportingTransition(
        predecessor=RegistrantIdentity("0000000001", "Predecessor Legal Name"),
        successor=RegistrantIdentity("0000000002", "Successor Legal Name"),
        effective_date=date(2024, 7, 10),
    )


def test_report_v2_promotes_verified_predecessor_wrapper_before_transition() -> None:
    evaluation = classify_report(
        _bundle(),
        "quarterly",
        transition=_transition(),
    )
    statuses = {status.fact_key: status for status in evaluation.statuses}

    assert evaluation.rule_version == "report-v2"
    assert statuses["predecessor-assets"].evidence_role == "selected_primary"
    assert "transition LegalEntityAxis wrapper" in (
        statuses["predecessor-assets"].selection_note or ""
    )
    assert statuses["segment-assets"].evidence_role == "dimensional"
    assert statuses["shell-assets"].evidence_role == "excluded"
    assert statuses["shell-assets"].exclusion_reasons[0][0] == ("TRANSITION_NON_REPORTING_ENTITY")


def test_report_v1_remains_unchanged_when_transition_metadata_exists() -> None:
    evaluation = classify_report(
        _bundle(),
        "quarterly",
        rule_version="report-v1",
        transition=_transition(),
    )
    statuses = {status.fact_key: status for status in evaluation.statuses}

    assert statuses["shell-assets"].evidence_role == "selected_primary"
    assert statuses["predecessor-assets"].evidence_role == "dimensional"
