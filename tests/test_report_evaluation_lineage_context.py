from __future__ import annotations

import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

from arelle.XmlValidateConst import VALID

from sec_inline_financials.company_lineage import (
    LineageMaintenanceService,
    RegistrantIdentity,
)
from sec_inline_financials.evidence_ingestion import EvidenceIngestionService
from sec_inline_financials.evidence_models import (
    ContextDimensionRecord,
    ContextRecord,
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
    ObservationRecord,
    ReportEvaluation,
    UnitRecord,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage import EvidenceStore


class _Resolver:
    def __init__(self, names: dict[str, str]) -> None:
        self._names = names

    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        return RegistrantIdentity(cik=cik, legal_name=self._names[cik])


class _ReuseOnlyProcessor:
    def __init__(self, profile: ExtractionProfile) -> None:
        self._profile = profile

    def extraction_profile(self) -> ExtractionProfile:
        return self._profile

    def extract_evidence(
        self,
        _company: Company,
        _filing: Filing,
        _capture_area: Path,
    ) -> FilingEvidenceBundle:
        raise AssertionError("the unchanged evidence snapshot must be reused")


class _BundleProcessor:
    def __init__(self, bundle: FilingEvidenceBundle) -> None:
        self._bundle = bundle

    def extraction_profile(self) -> ExtractionProfile:
        return self._bundle.extraction_profile

    def extract_evidence(
        self,
        company: Company,
        filing: Filing,
        _capture_area: Path,
    ) -> FilingEvidenceBundle:
        assert company == self._bundle.company
        assert filing == self._bundle.filing
        return self._bundle


def test_report_v2_is_recomputed_when_verified_lineage_context_changes(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence.sqlite3"
    store = EvidenceStore(database_path, tmp_path)
    store.initialize()
    predecessor = Company(ticker="TEST", cik="0000000001", name="Predecessor Corp")
    successor = Company(ticker="TEST", cik="0000000002", name="Successor Corp")
    store.create_processing_run(predecessor, purpose="seed", requested_window={})
    lineage = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(
            {
                predecessor.cik: predecessor.name,
                successor.cik: successor.name,
            }
        ),
    )
    store.apply_successor_promotion(
        lineage.preview_successor_promotion(predecessor.cik, successor.cik)
    )

    filing = Filing(
        accession="0000000002-26-000001",
        filing_date=date(2026, 5, 1),
        report_date=date(2026, 3, 31),
        form="10-K",
        primary_document="successor.htm",
        url="https://www.sec.gov/Archives/successor.htm",
        registrant_cik=successor.cik,
        archive_owner_cik=successor.cik,
    )
    run_id = store.create_processing_run(successor, purpose="test", requested_window={})
    attempt_id = store.begin_filing_attempt(run_id, successor, filing)
    evaluation = ReportEvaluation(
        report_kind="annual",
        rule_version="report-v2",
        report_date=filing.report_date,
        statuses=(),
    )
    bundle = _bundle(successor, filing)
    stored = store.save_snapshot(bundle, evaluation, attempt_id, ())
    store.publish_filing_window(
        successor,
        annual=[filing],
        quarterly=[],
        snapshot_ids={filing.accession: stored.snapshot_id},
        next_check_date_10k=date(2026, 6, 1),
        next_check_date_10q=date(2026, 6, 1),
    )
    original_ref = store.get_report_evaluation(stored.snapshot_id, "annual", "report-v2")
    assert original_ref is not None
    assert store.list_mapping_inputs("TEST", "annual", "report-v2")[0].report_evaluation_id == (
        original_ref.evaluation_id
    )

    store.apply_successor_promotion(
        lineage.preview_successor_promotion(
            predecessor.cik,
            successor.cik,
            effective_date=date(2026, 4, 1),
        )
    )

    assert store.get_report_evaluation(stored.snapshot_id, "annual", "report-v2") is None
    outcome = EvidenceIngestionService(
        sec_client=object(),  # type: ignore[arg-type]
        processor=_ReuseOnlyProcessor(bundle.extraction_profile),
        store=store,
    ).ingest_filing(successor, filing)
    refreshed_ref = store.get_report_evaluation(stored.snapshot_id, "annual", "report-v2")
    assert outcome.status == "reused"
    assert refreshed_ref is not None
    assert refreshed_ref.rule_version == "report-v2"
    assert refreshed_ref.evaluation_id != original_ref.evaluation_id
    assert store.load_report_evaluation(original_ref.evaluation_id).rule_version == "report-v2"
    assert store.list_mapping_inputs("TEST", "annual", "report-v2")[0].report_evaluation_id == (
        refreshed_ref.evaluation_id
    )
    with sqlite3.connect(database_path) as connection:
        stored_versions = tuple(
            row[0]
            for row in connection.execute(
                "SELECT rule_version FROM report_evaluations ORDER BY id"
            ).fetchall()
        )
    assert stored_versions[0] == "report-v2"
    assert stored_versions[1].startswith("report-v2@lineage:")


def test_reusable_report_v1_snapshot_gains_report_v2_without_reextracting(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path)
    store.initialize()
    company = Company(ticker="TEST", cik="0000000001", name="Test Company")
    filing = Filing(
        accession="0000000001-26-000001",
        filing_date=date(2026, 2, 1),
        report_date=date(2025, 12, 31),
        form="10-K",
        primary_document="test.htm",
        url="https://www.sec.gov/Archives/test.htm",
        registrant_cik=company.cik,
        archive_owner_cik=company.cik,
    )
    bundle = _bundle(company, filing)
    run_id = store.create_processing_run(company, purpose="seed", requested_window={})
    attempt_id = store.begin_filing_attempt(run_id, company, filing)
    stored = store.save_snapshot(
        bundle,
        ReportEvaluation(
            report_kind="annual",
            rule_version="report-v1",
            report_date=filing.report_date,
            statuses=(),
        ),
        attempt_id,
        (),
    )

    outcome = EvidenceIngestionService(
        sec_client=object(),  # type: ignore[arg-type]
        processor=_ReuseOnlyProcessor(bundle.extraction_profile),
        store=store,
    ).ingest_filing(company, filing)

    assert outcome.status == "reused"
    assert store.get_report_evaluation(stored.snapshot_id, "annual", "report-v1") is not None
    assert store.get_report_evaluation(stored.snapshot_id, "annual", "report-v2") is not None


def test_fresh_ingestion_applies_verified_transition_to_nonempty_evidence(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path)
    store.initialize()
    predecessor = Company(ticker="TEST", cik="0000000001", name="Predecessor Corp")
    successor = Company(ticker="TEST", cik="0000000002", name="Successor Corp")
    store.create_processing_run(predecessor, purpose="seed", requested_window={})
    lineage = LineageMaintenanceService(
        store=store,
        registrant_resolver=_Resolver(
            {
                predecessor.cik: predecessor.name,
                successor.cik: successor.name,
            }
        ),
    )
    store.apply_successor_promotion(
        lineage.preview_successor_promotion(
            predecessor.cik,
            successor.cik,
            effective_date=date(2026, 4, 1),
        )
    )
    filing = Filing(
        accession="0000000002-26-000002",
        filing_date=date(2026, 5, 1),
        report_date=date(2026, 3, 31),
        form="10-Q",
        primary_document="successor.htm",
        url="https://www.sec.gov/Archives/successor.htm",
        registrant_cik=successor.cik,
        archive_owner_cik=successor.cik,
    )
    bundle = _wrapped_fact_bundle(successor, filing, predecessor.name)

    outcome = EvidenceIngestionService(
        sec_client=object(),  # type: ignore[arg-type]
        processor=_BundleProcessor(bundle),
        store=store,
    ).ingest_filing(successor, filing)

    assert outcome.status == "stored"
    assert outcome.snapshot_id is not None
    ref = store.get_report_evaluation(outcome.snapshot_id, "quarterly", "report-v2")
    assert ref is not None
    evaluation = store.load_report_evaluation(ref.evaluation_id)
    assert evaluation.statuses[0].evidence_role == "selected_primary"
    assert evaluation.statuses[0].selection_note is not None
    assert "removed verified transition LegalEntityAxis wrapper" in (
        evaluation.statuses[0].selection_note
    )


def _bundle(company: Company, filing: Filing) -> FilingEvidenceBundle:
    return FilingEvidenceBundle(
        company=company,
        filing=filing,
        captured_company_name=company.name,
        captured_company_ticker=company.ticker,
        fiscal_year=2026,
        fiscal_period="FY",
        fiscal_year_source="test",
        fiscal_period_source="test",
        source_documents=(),
        concepts=(),
        concept_labels=(),
        contexts=(),
        units=(),
        observations=(),
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
            serialization_version="evidence-v3",
        ),
        coverage_manifest=CoverageManifest(
            recognized_fact_count=0,
            unresolved_observation_count=0,
            numeric_count=0,
            nonnumeric_count=0,
            nil_count=0,
            invalid_count=0,
            context_count=0,
            unit_count=0,
            validation_message_count=0,
            calculation_relationship_count=0,
            source_document_count=0,
        ),
        raw_log_json="{}",
    )


def _wrapped_fact_bundle(
    company: Company,
    filing: Filing,
    predecessor_name: str,
) -> FilingEvidenceBundle:
    context = ContextRecord(
        key="c1",
        source_order=0,
        source_document_key=None,
        source_locator="/xbrl/context[1]",
        raw_xml="<context id='c1'/>",
        period_kind="duration",
        xml_id="c1",
        entity_identifier=company.cik,
        period_start_date=date(2026, 1, 1),
        period_end_date=filing.report_date,
        dimensions=(
            ContextDimensionRecord(
                source_order=0,
                axis_namespace_uri="https://xbrl.sec.gov/dei/2024",
                axis_local_name="LegalEntityAxis",
                member_kind="explicit",
                context_element="segment",
                explicit_member_namespace_uri="https://xbrl.sec.gov/dei/2024",
                explicit_member_local_name=f"{predecessor_name} Member",
            ),
        ),
    )
    unit = UnitRecord(
        key="u1",
        source_order=0,
        source_document_key=None,
        source_locator="/xbrl/unit[1]",
        raw_xml="<unit id='u1'/>",
        legacy_report_unit_text="USD",
    )
    observation = ObservationRecord(
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
        unit_key=unit.key,
        raw_context_ref="c1",
        raw_unit_ref="u1",
        top_level_order=0,
        raw_value_text="100",
        typed_value_kind="decimal",
        typed_value_text="100",
        is_numeric=True,
        decimals="0",
    )
    base = _bundle(company, filing)
    return replace(
        base,
        contexts=(context,),
        units=(unit,),
        observations=(observation,),
        coverage_manifest=replace(
            base.coverage_manifest,
            recognized_fact_count=1,
            numeric_count=1,
            context_count=1,
            unit_count=1,
        ),
    )
