from __future__ import annotations

import hashlib
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from sec_inline_financials.errors import ArtifactHashMismatchError, SchemaError
from sec_inline_financials.evidence_classification import classify_report
from sec_inline_financials.evidence_ingestion import EvidenceIngestionService
from sec_inline_financials.evidence_models import (
    CalculationNetworkRecord,
    CalculationRelationshipRecord,
    ConceptRecord,
    ContextDimensionRecord,
    ContextRecord,
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
    ObservationRecord,
    SourceDocumentRecord,
    UnitMeasureRecord,
    UnitRecord,
    ValidationRecord,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.fingerprints import payload_hash, source_manifest_json
from sec_inline_financials.storage.recovery import (
    audit_backup,
    backup_evidence,
    recover_interrupted_attempts,
)
from sec_inline_financials.storage.report_projection import project_report

ASSETS = "{http://fasb.org/us-gaap/2025}Assets"
REVENUE = "{http://fasb.org/us-gaap/2025}Revenue"
CASH = "{http://fasb.org/us-gaap/2025}Cash"
ENTITY_NAME = "{http://xbrl.sec.gov/dei/2025}EntityRegistrantName"


def _bundle(tmp_path: Path) -> FilingEvidenceBundle:
    source_bytes = b"<html><body>inline filing</body></html>"
    source_path = tmp_path / "captured-primary.htm"
    source_path.write_bytes(source_bytes)
    company = Company(ticker="TEST", cik="0000000123", name="Test Company")
    filing = Filing(
        accession="0000000123-25-000001",
        filing_date=date(2025, 2, 1),
        report_date=date(2024, 12, 31),
        form="10-K",
        primary_document="test-2024.htm",
        url="https://www.sec.gov/Archives/edgar/data/123/filing/test-2024.htm",
    )
    instant = ContextRecord(
        key="context-instant",
        source_order=0,
        source_document_key="primary",
        source_locator="/html/context[1]",
        raw_xml="<context id='instant'/>",
        period_kind="instant",
        xml_id="instant",
        entity_scheme="https://www.sec.gov/CIK",
        entity_identifier="0000000123",
        period_end_date=filing.report_date,
        canonical_hash="1" * 64,
    )
    dimensional = ContextRecord(
        key="context-product",
        source_order=1,
        source_document_key="primary",
        source_locator="/html/context[2]",
        raw_xml="<context id='product'/>",
        period_kind="duration",
        xml_id="product",
        entity_scheme="https://www.sec.gov/CIK",
        entity_identifier="0000000123",
        period_start_date=date(2024, 1, 1),
        period_end_date=filing.report_date,
        canonical_hash="2" * 64,
        legacy_report_dimensions=("srt:ProductAxis=test:WidgetMember",),
        dimensions=(
            ContextDimensionRecord(
                source_order=0,
                axis_namespace_uri="http://fasb.org/srt/2025",
                axis_local_name="ProductAxis",
                member_kind="explicit",
                context_element="segment",
                explicit_member_namespace_uri="https://example.test/taxonomy",
                explicit_member_local_name="WidgetMember",
            ),
        ),
    )
    unit = UnitRecord(
        key="unit-usd",
        source_order=0,
        source_document_key="primary",
        source_locator="/html/unit[1]",
        raw_xml="<unit id='usd'/>",
        xml_id="usd",
        legacy_report_unit_text="iso4217:USD",
        canonical_hash="3" * 64,
        measures=(
            UnitMeasureRecord(
                side="numerator",
                measure_order=0,
                namespace_uri="http://www.xbrl.org/2003/iso4217",
                local_name="USD",
                display_qname="iso4217:USD",
            ),
        ),
    )

    def numeric(
        key: str,
        order: int,
        concept_key: str,
        qname: str,
        value: str | None,
        *,
        context_key: str = "context-instant",
        nil: bool = False,
        decimals: str = "-6",
    ) -> ObservationRecord:
        return ObservationRecord(
            key=key,
            source_order=order,
            observation_origin="recognized",
            fact_kind="item",
            source_document_key="primary",
            source_locator=f"/html/fact[{order + 1}]",
            is_nil=nil,
            validity_code=4,
            validity_name="valid",
            concept_key=concept_key,
            display_qname=qname,
            context_key=context_key,
            unit_key="unit-usd",
            raw_context_ref=context_key,
            raw_unit_ref="usd",
            top_level_order=order,
            raw_value_text=value,
            typed_value_kind="decimal" if value is not None else None,
            typed_value_text=value,
            is_numeric=True,
            decimals=decimals,
            legacy_report_label_text=qname.rsplit(":", maxsplit=1)[-1],
        )

    observations = (
        numeric("asset-precise", 0, ASSETS, "us-gaap:Assets", "0", decimals="-3"),
        numeric("asset-duplicate", 1, ASSETS, "us-gaap:Assets", "0", decimals="-6"),
        numeric(
            "revenue-dimensional",
            2,
            REVENUE,
            "us-gaap:Revenue",
            "100",
            context_key="context-product",
        ),
        numeric("cash-one", 3, CASH, "us-gaap:Cash", "10"),
        numeric("cash-two", 4, CASH, "us-gaap:Cash", "20"),
        numeric("assets-nil", 5, ASSETS, "us-gaap:Assets", None, nil=True),
        ObservationRecord(
            key="entity-name",
            source_order=6,
            observation_origin="recognized",
            fact_kind="item",
            source_document_key="primary",
            source_locator="/html/fact[7]",
            is_nil=False,
            validity_code=4,
            validity_name="valid",
            concept_key=ENTITY_NAME,
            display_qname="dei:EntityRegistrantName",
            context_key="context-instant",
            raw_context_ref="instant",
            top_level_order=6,
            raw_value_text="Test Company",
            typed_value_kind="string",
            typed_value_text="Test Company",
            is_numeric=False,
            legacy_report_label_text="Entity Registrant Name",
        ),
    )
    concepts = tuple(
        ConceptRecord(
            key=key,
            namespace_uri=key[1:].split("}", maxsplit=1)[0],
            local_name=key.split("}", maxsplit=1)[1],
            display_qname=qname,
            definition_status="resolved",
            is_numeric=numeric_flag,
        )
        for key, qname, numeric_flag in (
            (ASSETS, "us-gaap:Assets", True),
            (REVENUE, "us-gaap:Revenue", True),
            (CASH, "us-gaap:Cash", True),
            (ENTITY_NAME, "dei:EntityRegistrantName", False),
        )
    )
    return FilingEvidenceBundle(
        company=company,
        filing=filing,
        captured_company_name=company.name,
        captured_company_ticker=company.ticker,
        fiscal_year=2024,
        fiscal_period="FY",
        fiscal_year_source="fact:fy",
        fiscal_period_source="fact:fp",
        source_documents=(
            SourceDocumentRecord(
                key="primary",
                original_uri=filing.url,
                document_kind="inline XBRL instance",
                retention_kind="retained_original",
                captured_path=str(source_path),
                content_hash=hashlib.sha256(source_bytes).hexdigest(),
                byte_size=len(source_bytes),
                media_type="text/html",
            ),
        ),
        concepts=concepts,
        concept_labels=(),
        contexts=(instant, dimensional),
        units=(unit,),
        observations=observations,
        diagnostics=(),
        validation_messages=(
            ValidationRecord(
                message_order=0,
                level="warning",
                code="calc:inconsistency",
                message_text="Calculation differs",
                raw_record_json='{"code":"calc:inconsistency"}',
            ),
        ),
        calculation_networks=(
            CalculationNetworkRecord(
                arcrole_uri="http://www.xbrl.org/2003/arcrole/summation-item",
                extraction_status="extracted",
                relationship_count=1,
            ),
            CalculationNetworkRecord(
                arcrole_uri="https://xbrl.org/2023/arcrole/summation-item",
                extraction_status="extracted_empty",
                relationship_count=0,
            ),
        ),
        calculation_relationships=(
            CalculationRelationshipRecord(
                arcrole_uri="http://www.xbrl.org/2003/arcrole/summation-item",
                role_uri="Income Statement",
                relationship_order=0,
                parent_concept_key=ASSETS,
                child_concept_key=CASH,
                exact_weight_text="1",
            ),
        ),
        extraction_profile=ExtractionProfile(
            application_version="0.1.0",
            extractor_version="evidence-extractor-v1",
            arelle_version="2.41.7",
            validation_options=(("validate", "true"),),
            transform_plugin_revision="fixture",
            transform_plugin_hashes=(("plugin.py", "4" * 64),),
        ),
        coverage_manifest=CoverageManifest(
            recognized_fact_count=7,
            unresolved_observation_count=0,
            numeric_count=6,
            nonnumeric_count=1,
            nil_count=1,
            invalid_count=0,
            context_count=2,
            unit_count=1,
            validation_message_count=1,
            calculation_relationship_count=1,
            source_document_count=1,
        ),
        raw_log_json='{"log":[{"code":"calc:inconsistency"}]}',
    )


def _installed_artifacts(store: EvidenceStore, bundle: FilingEvidenceBundle, attempt: int):
    source = bundle.source_documents[0]
    source_artifact = store.artifacts.install(
        Path(source.captured_path or ""),
        purpose="source-document",
        logical_name="source-0000",
        media_type="text/html",
        source_document_key=source.key,
    )
    log_path = store.artifacts.stage_bytes(
        str(attempt), "arelle-log.json", bundle.raw_log_json.encode()
    )
    log_artifact = store.artifacts.install(
        log_path,
        purpose="arelle-log",
        logical_name="arelle-log.json",
        media_type="application/json",
    )
    manifest_path = store.artifacts.stage_bytes(
        str(attempt), "source-manifest.json", source_manifest_json(bundle).encode()
    )
    manifest_artifact = store.artifacts.install(
        manifest_path,
        purpose="source-manifest",
        logical_name="source-manifest.json",
        media_type="application/json",
    )
    return source_artifact, log_artifact, manifest_artifact


def test_snapshot_round_trip_preserves_all_observations_and_report_decisions(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)
    evaluation = classify_report(bundle, "annual")
    roles = {status.fact_key: status.evidence_role for status in evaluation.statuses}
    assert roles["asset-precise"] == "selected_primary"
    assert roles["asset-duplicate"] == "duplicate_not_selected"
    assert roles["revenue-dimensional"] == "dimensional"
    assert roles["cash-one"] == roles["cash-two"] == "conflict_candidate"
    assert roles["assets-nil"] == roles["entity-name"] == "excluded"

    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    assert store.initialize() == store.initialize() == 2
    run = store.create_processing_run(bundle.company, purpose="test", requested_window={})
    attempt = store.begin_filing_attempt(run, bundle.company, bundle.filing)
    result = store.save_snapshot(
        bundle, evaluation, attempt, _installed_artifacts(store, bundle, attempt)
    )
    assert result.disposition == "stored"
    assert store.finish_processing_run(run) == "succeeded"

    reloaded = store.load_snapshot(result.snapshot_id)
    reloaded_evaluation = store.load_report_evaluation(
        store.get_report_evaluation(result.snapshot_id, "annual", "report-v1").evaluation_id  # type: ignore[union-attr]
    )
    assert payload_hash(reloaded) == payload_hash(bundle)
    assert reloaded_evaluation == evaluation
    assert len(store.list_facts(result.snapshot_id).items) == 7
    assert store.audit_snapshot(result.snapshot_id)["ok"] is True

    projected = project_report(reloaded, reloaded_evaluation)
    assert [fact.value for fact in projected.primary_facts] == [0]
    assert len(projected.dimensional_facts) == 1
    assert len(projected.reconciliation_issues) == 1

    second_run = store.create_processing_run(bundle.company, purpose="test", requested_window={})
    second_attempt = store.begin_filing_attempt(second_run, bundle.company, bundle.filing)
    reused = store.save_snapshot(
        bundle,
        evaluation,
        second_attempt,
        _installed_artifacts(store, bundle, second_attempt),
    )
    assert reused.snapshot_id == result.snapshot_id
    assert reused.disposition == "reused"
    with store.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 7


def test_failure_during_snapshot_insert_rolls_back_every_evidence_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)
    evaluation = classify_report(bundle, "annual")
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    store.initialize()
    run = store.create_processing_run(bundle.company, purpose="test", requested_window={})
    attempt = store.begin_filing_attempt(run, bundle.company, bundle.filing)

    def fail(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("injected insert failure")

    monkeypatch.setattr(store, "_insert_snapshot_evidence", fail)
    with pytest.raises(RuntimeError, match="injected"):
        store.save_snapshot(
            bundle, evaluation, attempt, _installed_artifacts(store, bundle, attempt)
        )
    with store.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM evidence_snapshots").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM facts").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM artifacts").fetchone()[0] == 0


def test_modified_migration_and_corrupt_artifact_fail_explicitly(tmp_path: Path) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    store.initialize()
    with store.database.write_transaction() as connection:
        connection.execute("UPDATE schema_migrations SET checksum = ?", ("0" * 64,))
    with pytest.raises(SchemaError, match="does not match"):
        store.initialize()

    staged = store.artifacts.stage_bytes("artifact-test", "source.bin", b"original")
    artifact = store.artifacts.install(staged, purpose="test", logical_name="source.bin")
    object_path = store.artifacts.root / artifact.relative_object_path
    object_path.write_bytes(b"corrupt")
    with pytest.raises(ArtifactHashMismatchError):
        store.artifacts.resolve(artifact.relative_object_path, artifact.sha256, artifact.byte_size)


def test_database_enforces_snapshot_scoped_fact_links(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    store.initialize()
    run = store.create_processing_run(bundle.company, purpose="test", requested_window={})
    attempt = store.begin_filing_attempt(run, bundle.company, bundle.filing)
    result = store.save_snapshot(
        bundle,
        classify_report(bundle, "annual"),
        attempt,
        _installed_artifacts(store, bundle, attempt),
    )
    with store.database.connection() as connection:
        fact_id = connection.execute("SELECT id FROM facts LIMIT 1").fetchone()[0]
        evaluation_id = connection.execute("SELECT id FROM report_evaluations LIMIT 1").fetchone()[
            0
        ]
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO fact_report_status("
                "evaluation_id, snapshot_id, fact_id, evidence_role"
                ") VALUES (?, ?, ?, 'excluded')",
                (evaluation_id, result.snapshot_id + 1, fact_id),
            )
        connection.execute("ROLLBACK")


def test_explicit_ingestion_reuses_compatible_snapshot_without_writing_report(
    tmp_path: Path,
) -> None:
    bundle = _bundle(tmp_path)

    class FakeProcessor:
        calls = 0

        def extraction_profile(self) -> ExtractionProfile:
            return bundle.extraction_profile

        def extract_evidence(
            self, company: Company, filing: Filing, capture_area: Path
        ) -> FilingEvidenceBundle:
            assert company == bundle.company
            assert filing == bundle.filing
            assert capture_area.name.isdigit()
            self.calls += 1
            return bundle

    processor = FakeProcessor()
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    store.initialize()
    service = EvidenceIngestionService(
        sec_client=object(),  # type: ignore[arg-type]
        processor=processor,
        store=store,
    )

    first = service.ingest_filing(bundle.company, bundle.filing)
    second = service.ingest_filing(bundle.company, bundle.filing)

    assert first.status == "stored"
    assert second.status == "reused"
    assert second.snapshot_id == first.snapshot_id
    assert processor.calls == 1
    assert list(tmp_path.rglob("*.txt")) == []


def test_backup_restores_snapshot_and_recovery_defers_live_processes(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    store.initialize()
    run = store.create_processing_run(bundle.company, purpose="test", requested_window={})
    attempt = store.begin_filing_attempt(run, bundle.company, bundle.filing)

    assert recover_interrupted_attempts(store, process_is_alive=lambda _pid: True) == ()
    assert recover_interrupted_attempts(store, process_is_alive=lambda _pid: False) == (attempt,)
    assert store.finish_processing_run(run) == "failed"

    stored_run = store.create_processing_run(bundle.company, purpose="test", requested_window={})
    stored_attempt = store.begin_filing_attempt(stored_run, bundle.company, bundle.filing)
    result = store.save_snapshot(
        bundle,
        classify_report(bundle, "annual"),
        stored_attempt,
        _installed_artifacts(store, bundle, stored_attempt),
    )
    store.finish_processing_run(stored_run)
    manifest = backup_evidence(store, tmp_path / "backup")

    assert manifest.is_file()
    audit = audit_backup(tmp_path / "backup")
    assert audit["ok"] is True
    restored = EvidenceStore(tmp_path / "backup" / "evidence.sqlite3", tmp_path / "backup")
    assert payload_hash(restored.load_snapshot(result.snapshot_id)) == payload_hash(bundle)


def test_ingestion_recovers_committed_snapshot_when_cleanup_return_is_uncertain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path)

    class FakeProcessor:
        def extraction_profile(self) -> ExtractionProfile:
            return bundle.extraction_profile

        def extract_evidence(
            self, _company: Company, _filing: Filing, _capture_area: Path
        ) -> FilingEvidenceBundle:
            return bundle

    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    store.initialize()
    monkeypatch.setattr(
        store.artifacts,
        "cleanup_staging",
        lambda _identity: (_ for _ in ()).throw(OSError("uncertain return")),
    )
    service = EvidenceIngestionService(
        sec_client=object(),  # type: ignore[arg-type]
        processor=FakeProcessor(),
        store=store,
    )

    outcome = service.ingest_filing(bundle.company, bundle.filing)

    assert outcome.status == "stored"
    assert outcome.snapshot_id is not None
    assert store.audit_snapshot(outcome.snapshot_id)["ok"] is True


def test_failed_extraction_registers_raw_arelle_log(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    class FailingProcessor:
        def extraction_profile(self) -> ExtractionProfile:
            return bundle.extraction_profile

        def extract_evidence(
            self, _company: Company, _filing: Filing, capture_area: Path
        ) -> FilingEvidenceBundle:
            (capture_area / "raw-arelle-log.json").write_text(
                '{"log":[{"level":"error"}]}', encoding="utf-8"
            )
            raise RuntimeError("extraction failed")

    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "runtime")
    store.initialize()
    service = EvidenceIngestionService(
        sec_client=object(),  # type: ignore[arg-type]
        processor=FailingProcessor(),
        store=store,
    )

    outcome = service.ingest_filing(bundle.company, bundle.filing)

    assert outcome.status == "failed"
    with store.database.connection() as connection:
        row = connection.execute(
            "SELECT aa.purpose, a.relative_object_path FROM attempt_artifacts AS aa "
            "JOIN artifacts AS a ON a.id = aa.artifact_id"
        ).fetchone()
    assert row["purpose"] == "failed-arelle-log"
    assert store.artifacts.resolve(
        str(row["relative_object_path"]),
        hashlib.sha256(b'{"log":[{"level":"error"}]}').hexdigest(),
        len(b'{"log":[{"level":"error"}]}'),
    ).is_file()
