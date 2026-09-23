from __future__ import annotations

import hashlib
import sqlite3
import zlib
from dataclasses import replace
from datetime import date
from importlib import resources
from pathlib import Path

import pytest

from sec_inline_financials.company_purge import CompanyDataPurger
from sec_inline_financials.errors import EvidenceStorageError
from sec_inline_financials.evidence_models import (
    ConceptRecord,
    CoverageManifest,
    ExtractionProfile,
    FactReportStatus,
    FilingEvidenceBundle,
    ObservationRecord,
    ReconciliationIssueRecord,
    ReportEvaluation,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage import EvidenceStore


def _normalized_migration_bytes(raw: bytes) -> bytes:
    return raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n")


def test_schema_five_database_upgrades_to_compressed_text_payloads(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence.sqlite3"
    package = resources.files("sec_inline_financials.storage.sql")
    migrations = sorted(
        resource
        for resource in package.iterdir()
        if resource.name.endswith(".sql") and int(resource.name.split("_", 1)[0]) <= 5
    )
    with sqlite3.connect(database_path) as connection:
        for version, migration in enumerate(migrations, start=1):
            raw = _normalized_migration_bytes(migration.read_bytes())
            connection.executescript(raw.decode("utf-8"))
            connection.execute(
                "INSERT INTO schema_migrations(version, filename, checksum, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (
                    version,
                    migration.name,
                    hashlib.sha256(raw).hexdigest(),
                    "2026-01-01T00:00:00+00:00",
                ),
            )
        connection.commit()

    store = EvidenceStore(database_path, tmp_path)

    assert store.initialize() == 6
    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM text_payloads").fetchone() == (0,)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(facts)").fetchall()}
        assert "raw_value_payload_sha256" in columns


def test_only_identical_string_payload_is_compacted_and_public_reads_reconstruct_it(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "evidence.sqlite3"
    store = EvidenceStore(database_path, tmp_path)
    assert store.initialize() == 6
    company = Company(ticker="TEST", cik="0000000001", name="Test Company")
    filing = Filing(
        accession="0000000001-26-000001",
        filing_date=date(2026, 1, 2),
        report_date=date(2025, 12, 31),
        form="10-K",
        primary_document="test.htm",
        url="https://www.sec.gov/Archives/test.htm",
    )
    text = "Management discussion " * 1_000
    compacted = ObservationRecord(
        key="fact-1",
        source_order=0,
        observation_origin="recognized",
        fact_kind="item",
        source_document_key=None,
        source_locator="/html/body/div[1]",
        is_nil=False,
        validity_code=4,
        validity_name="valid",
        raw_value_text=text,
        typed_value_kind="string",
        typed_value_text=text,
        is_numeric=False,
        concept_key="concept-1",
        display_qname="test:TextBlock",
    )
    normalized = ObservationRecord(
        key="fact-2",
        source_order=1,
        observation_origin="recognized",
        fact_kind="item",
        source_document_key=None,
        source_locator="/html/body/div[2]",
        is_nil=False,
        validity_code=4,
        validity_name="valid",
        raw_value_text="  normalized text  ",
        typed_value_kind="string",
        typed_value_text="normalized text",
        is_numeric=False,
    )
    numeric = ObservationRecord(
        key="fact-3",
        source_order=2,
        observation_origin="recognized",
        fact_kind="item",
        source_document_key=None,
        source_locator="/html/body/div[3]",
        is_nil=False,
        validity_code=4,
        validity_name="valid",
        raw_value_text="42",
        typed_value_kind="decimal",
        typed_value_text="42",
        is_numeric=True,
    )
    repeated = replace(
        compacted,
        key="fact-4",
        source_order=3,
        source_locator="/html/body/div[4]",
    )
    bundle = FilingEvidenceBundle(
        company=company,
        filing=filing,
        captured_company_name=company.name,
        captured_company_ticker=company.ticker,
        fiscal_year=2025,
        fiscal_period="FY",
        fiscal_year_source="test",
        fiscal_period_source="test",
        source_documents=(),
        concepts=(
            ConceptRecord(
                key="concept-1",
                namespace_uri="https://example.test/2026",
                local_name="TextBlock",
                display_qname="test:TextBlock",
                definition_status="defined",
                period_type="duration",
                is_numeric=False,
                is_abstract=False,
            ),
        ),
        concept_labels=(),
        contexts=(),
        units=(),
        observations=(compacted, normalized, numeric, repeated),
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
            serialization_version="evidence-v2",
        ),
        coverage_manifest=CoverageManifest(
            recognized_fact_count=4,
            unresolved_observation_count=0,
            numeric_count=1,
            nonnumeric_count=3,
            nil_count=0,
            invalid_count=0,
            context_count=0,
            unit_count=0,
            validation_message_count=0,
            calculation_relationship_count=0,
            source_document_count=0,
        ),
        raw_log_json="",
    )
    evaluation = ReportEvaluation(
        report_kind="annual",
        rule_version="report-v1",
        report_date=filing.report_date,
        statuses=tuple(
            FactReportStatus(
                fact_key=observation.key,
                evidence_role="excluded",
                exclusion_reasons=(("TEST_EXCLUSION", "test fixture"),),
            )
            for observation in (compacted, normalized, numeric, repeated)
        ),
        reconciliation_issues=(
            ReconciliationIssueRecord(
                concept_key="concept-1",
                reason_code="TEST_CONFLICT",
                reason_text="test fixture",
                issue_order=0,
                candidate_fact_keys=(compacted.key,),
            ),
        ),
    )
    run_id = store.create_processing_run(company, purpose="test", requested_window={})
    attempt_id = store.begin_filing_attempt(run_id, company, filing)

    result = store.save_snapshot(bundle, evaluation, attempt_id, ())

    with sqlite3.connect(database_path) as connection:
        stored = connection.execute(
            "SELECT observation_key, raw_value_text, raw_value_payload_sha256, "
            "typed_value_text FROM facts "
            "WHERE snapshot_id = ? ORDER BY source_order",
            (result.snapshot_id,),
        ).fetchall()
        payload = connection.execute(
            "SELECT sha256, text_encoding, compression_codec, original_byte_size, "
            "compressed_byte_size, compressed_bytes FROM text_payloads"
        ).fetchone()
    text_bytes = text.encode("utf-8")
    text_hash = hashlib.sha256(text_bytes).hexdigest()
    assert stored == [
        ("fact-1", None, text_hash, None),
        ("fact-2", "  normalized text  ", None, "normalized text"),
        ("fact-3", "42", None, "42"),
        ("fact-4", None, text_hash, None),
    ]
    assert payload is not None
    assert payload[:5] == (text_hash, "utf-8", "zlib", len(text_bytes), len(payload[5]))
    assert len(payload[5]) < len(text_bytes)
    assert zlib.decompress(payload[5]) == text_bytes
    loaded = {
        observation.key: observation
        for observation in store.load_snapshot(result.snapshot_id).observations
    }
    assert loaded["fact-1"].raw_value_text == text
    assert loaded["fact-1"].typed_value_text == text
    assert loaded["fact-4"].typed_value_text == text
    assert loaded["fact-2"].typed_value_text == "normalized text"
    assert loaded["fact-3"].typed_value_text == "42"
    listed = {item["observation_key"]: item for item in store.list_facts(result.snapshot_id).items}
    assert listed["fact-1"]["typed_value_text"] == text
    assert listed["fact-2"]["typed_value_text"] == "normalized text"
    assert listed["fact-3"]["typed_value_text"] == "42"
    detailed = store.get_fact(int(listed["fact-1"]["id"]))
    assert detailed["typed_value_text"] == text
    conflict = store.get_conflict(1)
    assert conflict["candidates"][0]["raw_value_text"] == text
    assert conflict["candidates"][0]["typed_value_text"] == text
    audit = store.audit_snapshot(result.snapshot_id)
    assert audit["text_payloads"] == {
        "references": 2,
        "distinct_payloads": 1,
        "original_bytes": len(text_bytes),
        "compressed_bytes": len(payload[5]),
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "UPDATE text_payloads SET compressed_byte_size = 3, compressed_bytes = ?",
            (b"bad",),
        )
    with pytest.raises(EvidenceStorageError, match="Could not decompress text payload"):
        store.audit_snapshot(result.snapshot_id)
    assert store.finish_processing_run(run_id) == "succeeded"

    CompanyDataPurger(store).purge((company.ticker,))

    with sqlite3.connect(database_path) as connection:
        assert connection.execute("SELECT count(*) FROM text_payloads").fetchone() == (0,)
