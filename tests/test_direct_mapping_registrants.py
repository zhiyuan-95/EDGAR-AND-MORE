import json
import sqlite3
from dataclasses import replace
from datetime import date
from pathlib import Path

from sec_inline_financials.direct_mapping import resolve_metric_window
from sec_inline_financials.mapping_models import (
    ConceptIdentity,
    MappingFactInput,
    MappingSnapshotInput,
    MetricResult,
    active_window_hash,
)
from sec_inline_financials.mapping_rules import DIRECT_MAPPING_RULES
from sec_inline_financials.storage.evidence_store import EvidenceStore


def _snapshot(registrant_ciks: tuple[str, ...]) -> MappingSnapshotInput:
    return MappingSnapshotInput(
        snapshot_id=11,
        filing_id=7,
        accession="0000034088-26-000093",
        form="10-Q",
        report_date=date(2026, 6, 30),
        active_window_rank=1,
        payload_hash="payload",
        report_evaluation_id=19,
        source_report_rule_version="report-v1",
        company_cik="0002115436",
        registrant_ciks=registrant_ciks,
    )


def _revenue_fact(snapshot: MappingSnapshotInput, entity_identifier: str) -> MappingFactInput:
    return MappingFactInput(
        snapshot_id=snapshot.snapshot_id,
        fact_id=23,
        concept=ConceptIdentity(
            namespace_family="us-gaap",
            namespace_uri="http://fasb.org/us-gaap/2026",
            local_name="Revenues",
            display_qname="us-gaap:Revenues",
        ),
        concept_period_type="duration",
        context_period_kind="duration",
        entity_identifier=entity_identifier,
        period_start_date=date(2026, 4, 1),
        period_end_date=snapshot.report_date,
        unit_family="monetary",
        typed_value_text="116017000000",
        evidence_role="selected_primary",
    )


def _resolve_revenue(snapshot: MappingSnapshotInput, fact: MappingFactInput) -> MetricResult:
    evaluation = resolve_metric_window(
        rule_set=DIRECT_MAPPING_RULES,
        report_kind="quarterly",
        snapshots=(snapshot,),
        observed_concepts=frozenset({fact.concept}),
        candidate_facts=(fact,),
    )
    return next(result for result in evaluation.results if result.metric_key == "revenue")


def test_direct_mapping_accepts_fact_for_declared_filing_registrant() -> None:
    snapshot = _snapshot(("0000034088", "0002115436"))
    fact = _revenue_fact(snapshot, "0000034088")

    revenue_result = _resolve_revenue(snapshot, fact)

    assert revenue_result.status == "reported"
    assert revenue_result.selected_fact_id == fact.fact_id


def test_direct_mapping_rejects_fact_for_undeclared_entity() -> None:
    snapshot = _snapshot(("0000034088", "0002115436"))
    fact = _revenue_fact(snapshot, "0009999999")

    revenue_result = _resolve_revenue(snapshot, fact)

    assert revenue_result.status == "missing"
    trace = json.loads(revenue_result.resolution_trace_json)
    assert trace["candidates"][1]["snapshot_evidence"][0]["compatibility_failures"] == [
        "entity_mismatch"
    ]


def test_explicit_filing_registrant_is_authoritative_for_lineage_mapping() -> None:
    snapshot = replace(
        _snapshot(("0000034088", "0002115436")),
        registrant_cik="0000034088",
    )

    predecessor = _resolve_revenue(snapshot, _revenue_fact(snapshot, "0000034088"))
    canonical_current = _resolve_revenue(snapshot, _revenue_fact(snapshot, "0002115436"))

    assert predecessor.status == "reported"
    assert canonical_current.status == "missing"


def test_active_window_hash_includes_declared_registrants() -> None:
    snapshot = _snapshot(("0002115436",))
    expanded = replace(snapshot, registrant_ciks=("0000034088", "0002115436"))

    assert active_window_hash((snapshot,)) != active_window_hash((expanded,))


def test_mapping_inputs_load_dei_declared_registrants(tmp_path: Path) -> None:
    database_path = tmp_path / "evidence.sqlite3"
    store = EvidenceStore(database_path)
    store.initialize()
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            "INSERT INTO companies(id, cik, ticker, current_name, created_at, updated_at) "
            "VALUES (1, '0002115436', 'XOM', 'Exxon Mobil Corporation', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO company_ciks(company_id, cik, legal_name, associated_at, updated_at) "
            "VALUES (1, '0002115436', 'Exxon Mobil Corporation', 'now', 'now')"
        )
        connection.execute(
            "INSERT INTO filings(id, company_id, accession, form, filing_date, report_date, "
            "primary_document, source_url, is_active, active_window_rank) "
            "VALUES (1, 1, '0000034088-26-000093', '10-Q', '2026-08-03', '2026-06-30', "
            "'xom-20260630.htm', 'https://www.sec.gov/example', 1, 1)"
        )
        connection.execute(
            "INSERT INTO filing_provenance("
            "filing_id, registrant_cik, archive_owner_cik, recorded_at"
            ") VALUES (1, '0002115436', '0002115436', 'now')"
        )
        connection.execute(
            "INSERT INTO processing_runs(id, company_id, purpose, requested_window_json, "
            "owner_pid, owner_process_start_identity, started_at, status) "
            "VALUES (1, 1, 'test', '{}', 1, 'test', 'now', 'running')"
        )
        connection.execute(
            "INSERT INTO processing_run_filings(id, run_id, filing_id, status, started_at) "
            "VALUES (1, 1, 1, 'processing', 'now')"
        )
        connection.execute(
            "INSERT INTO evidence_snapshots(id, filing_id, completed_by_attempt_id, "
            "captured_company_name, captured_company_ticker, "
            "captured_filing_metadata_json, source_manifest_hash, extraction_profile_hash, "
            "extraction_profile_json, payload_hash, coverage_manifest_json, captured_at) "
            "VALUES (1, 1, 1, 'Exxon Mobil Corporation', 'XOM', '{}', ?, ?, '{}', ?, '{}', "
            "'now')",
            ("a" * 64, "b" * 64, "c" * 64),
        )
        connection.execute(
            "UPDATE processing_run_filings SET snapshot_id = 1, status = 'stored' WHERE id = 1"
        )
        connection.execute(
            "INSERT INTO active_filing_snapshots(filing_id, snapshot_id, published_at) "
            "VALUES (1, 1, 'now')"
        )
        connection.execute(
            "INSERT INTO report_evaluations(id, snapshot_id, report_kind, rule_version, "
            "report_date, evaluated_at) "
            "VALUES (1, 1, 'quarterly', 'report-v1', '2026-06-30', 'now')"
        )
        connection.execute(
            "INSERT INTO concepts(id, namespace_uri, local_name) "
            "VALUES (1, 'http://xbrl.sec.gov/dei/2026', 'EntityCentralIndexKey')"
        )
        connection.execute(
            "INSERT INTO snapshot_concepts(snapshot_id, concept_id, concept_key, "
            "display_qname, definition_status) "
            "VALUES (1, 1, '{http://xbrl.sec.gov/dei/2026}EntityCentralIndexKey', "
            "'dei:EntityCentralIndexKey', 'defined')"
        )
        for source_order, registrant_cik in enumerate(("0000034088", "0002115436")):
            connection.execute(
                "INSERT INTO facts(id, snapshot_id, observation_key, source_order, "
                "observation_origin, fact_kind, source_locator, is_nil, validity_name, "
                "concept_id, display_qname, raw_value_text, typed_value_kind) "
                "VALUES (?, 1, ?, ?, 'recognized', 'item', ?, 0, 'valid', 1, "
                "'dei:EntityCentralIndexKey', ?, 'string')",
                (
                    source_order + 1,
                    f"registrant-{source_order}",
                    source_order,
                    f"line-{source_order}",
                    registrant_cik,
                ),
            )

    snapshots = store.list_mapping_inputs("XOM", "quarterly", "report-v1")

    assert snapshots[0].registrant_ciks == ("0000034088", "0002115436")
