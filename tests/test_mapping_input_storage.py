from __future__ import annotations

from pathlib import Path

import pytest

from sec_inline_financials.storage import EvidenceStore

_NOW = "2026-01-01T00:00:00+00:00"
_DEI_NAMESPACE = "https://xbrl.sec.gov/dei/2026"
_HASH_A = "a" * 64
_HASH_B = "b" * 64


def test_list_mapping_inputs_batches_registrant_identity_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path / "artifacts")
    store.initialize()
    _insert_active_mapping_window(store)

    statements: list[str] = []
    original_connect = store.database.connect

    def traced_connect():
        connection = original_connect()
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(store.database, "connect", traced_connect)
    monkeypatch.setattr(store, "_verify_snapshot_artifacts", lambda _connection, _snapshot_id: ())

    inputs = store.list_mapping_inputs("test", "annual", "report-v1")

    assert [(item.snapshot_id, item.registrant_cik, item.registrant_ciks) for item in inputs] == [
        (1, "0000000001", ("0000000001", "0000000002")),
        (2, "0000000002", ("0000000002",)),
    ]
    select_count = sum(
        1 for statement in statements if statement.lstrip().upper().startswith("SELECT")
    )
    assert select_count <= 5


def _insert_active_mapping_window(store: EvidenceStore) -> None:
    with store.database.write_transaction() as connection:
        connection.execute(
            "INSERT INTO companies(id, cik, ticker, current_name, created_at, updated_at) "
            "VALUES (1, '0000000002', 'TEST', 'Test Company', ?, ?)",
            (_NOW, _NOW),
        )
        connection.executemany(
            "INSERT INTO company_ciks(company_id, cik, legal_name, associated_at, updated_at) "
            "VALUES (1, ?, ?, ?, ?)",
            (
                ("0000000001", "Old Test Company", _NOW, _NOW),
                ("0000000002", "Test Company", _NOW, _NOW),
            ),
        )
        connection.execute(
            "INSERT INTO processing_runs("
            "id, company_id, purpose, requested_window_json, owner_pid, "
            "owner_process_start_identity, started_at, status"
            ") VALUES (1, 1, 'test', '{}', 1, 'test', ?, 'running')",
            (_NOW,),
        )
        connection.execute(
            "INSERT INTO concepts(id, namespace_uri, local_name) "
            "VALUES (1, ?, 'EntityCentralIndexKey')",
            (_DEI_NAMESPACE,),
        )
        for index, (registrant_cik, report_date) in enumerate(
            (
                ("0000000001", "2024-12-31"),
                ("0000000002", "2025-12-31"),
            ),
            start=1,
        ):
            _insert_snapshot(
                connection,
                filing_id=index,
                snapshot_id=index,
                attempt_id=index,
                report_evaluation_id=100 + index,
                accession=f"0000000002-26-00000{index}",
                report_date=report_date,
                rank=index,
                registrant_cik=registrant_cik,
            )
        _insert_dei_fact(connection, snapshot_id=1, source_order=0, value="0000000002")
        _insert_dei_fact(connection, snapshot_id=1, source_order=1, value="0000000099")


def _insert_snapshot(
    connection,
    *,
    filing_id: int,
    snapshot_id: int,
    attempt_id: int,
    report_evaluation_id: int,
    accession: str,
    report_date: str,
    rank: int,
    registrant_cik: str,
) -> None:
    connection.execute(
        "INSERT INTO filings("
        "id, company_id, accession, form, filing_date, report_date, primary_document, "
        "source_url, is_active, active_window_rank"
        ") VALUES (?, 1, ?, '10-K', '2026-02-01', ?, 'test.htm', "
        "'https://www.sec.gov/test.htm', 1, ?)",
        (filing_id, accession, report_date, rank),
    )
    connection.execute(
        "INSERT INTO filing_provenance("
        "filing_id, company_id, registrant_cik, archive_owner_cik, recorded_at"
        ") VALUES (?, 1, ?, '0000000002', ?)",
        (filing_id, registrant_cik, _NOW),
    )
    connection.execute(
        "INSERT INTO processing_run_filings("
        "id, run_id, filing_id, status, started_at, completed_at"
        ") VALUES (?, 1, ?, 'processing', ?, ?)",
        (attempt_id, filing_id, _NOW, _NOW),
    )
    connection.execute(
        "INSERT INTO evidence_snapshots("
        "id, filing_id, completed_by_attempt_id, captured_company_name, "
        "captured_company_ticker, captured_filing_metadata_json, fiscal_year, "
        "fiscal_period, fiscal_year_source, fiscal_period_source, source_manifest_hash, "
        "extraction_profile_hash, extraction_profile_json, payload_hash, "
        "coverage_manifest_json, captured_at"
        ") VALUES (?, ?, ?, 'Test Company', 'TEST', '{}', 2026, 'FY', 'fixture', "
        "'fixture', ?, ?, '{}', ?, '{}', ?)",
        (snapshot_id, filing_id, attempt_id, _HASH_A, _HASH_B, f"{snapshot_id:064x}", _NOW),
    )
    connection.execute(
        "UPDATE processing_run_filings SET status = 'stored', snapshot_id = ? WHERE id = ?",
        (snapshot_id, attempt_id),
    )
    connection.execute(
        "INSERT INTO active_filing_snapshots(filing_id, snapshot_id, published_at) "
        "VALUES (?, ?, ?)",
        (filing_id, snapshot_id, _NOW),
    )
    connection.execute(
        "INSERT INTO report_evaluations("
        "id, snapshot_id, report_kind, rule_version, report_date, evaluated_at"
        ") VALUES (?, ?, 'annual', 'report-v1', ?, ?)",
        (report_evaluation_id, snapshot_id, report_date, _NOW),
    )
    connection.execute(
        "INSERT INTO snapshot_concepts("
        "snapshot_id, concept_id, concept_key, display_qname, definition_status"
        ") VALUES (?, 1, 'dei-entity-cik', 'dei:EntityCentralIndexKey', 'defined')",
        (snapshot_id,),
    )


def _insert_dei_fact(connection, *, snapshot_id: int, source_order: int, value: str) -> None:
    connection.execute(
        "INSERT INTO facts("
        "snapshot_id, observation_key, source_order, observation_origin, fact_kind, "
        "source_locator, is_nil, validity_name, concept_id, display_qname, "
        "raw_value_text, typed_value_kind, typed_value_text, is_numeric"
        ") VALUES (?, ?, ?, 'recognized', 'item', '/html/body', 0, 'valid', 1, "
        "'dei:EntityCentralIndexKey', ?, 'string', ?, 0)",
        (snapshot_id, f"dei-cik-{source_order}", source_order, value, value),
    )
