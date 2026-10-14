from __future__ import annotations

import hashlib
from pathlib import Path

from sec_inline_financials.company_purge import CompanyDataPurger
from sec_inline_financials.storage import EvidenceStore

_NOW = "2026-10-12T12:00:00+00:00"


def _install_test_object(root: Path, content: bytes) -> tuple[str, str, int]:
    digest = hashlib.sha256(content).hexdigest()
    relative_path = f"objects/sha256/{digest[:2]}/{digest}"
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return digest, relative_path, len(content)


def test_purge_removes_every_lineage_artifact_but_preserves_cross_company_shared_files(
    tmp_path: Path,
) -> None:
    store = EvidenceStore(tmp_path / "evidence.sqlite3", tmp_path)
    store.initialize()
    predecessor_hash, predecessor_path, predecessor_size = _install_test_object(
        tmp_path, b"predecessor filing artifact"
    )
    successor_hash, successor_path, successor_size = _install_test_object(
        tmp_path, b"successor filing artifact"
    )
    shared_hash, shared_path, shared_size = _install_test_object(
        tmp_path, b"shared taxonomy artifact"
    )

    with store.database.write_transaction() as connection:
        connection.executemany(
            "INSERT INTO companies(id, cik, ticker, current_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                (1, "0000000002", "LINE", "Lineage Company", _NOW, _NOW),
                (2, "0000000003", "OTHER", "Other Company", _NOW, _NOW),
            ),
        )
        connection.executemany(
            "INSERT INTO company_ciks(company_id, cik, legal_name, associated_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                (1, "0000000001", "Lineage Predecessor", _NOW, _NOW),
                (1, "0000000002", "Lineage Successor", _NOW, _NOW),
                (2, "0000000003", "Other Company", _NOW, _NOW),
            ),
        )
        connection.execute(
            "INSERT INTO company_cik_transitions("
            "company_id, predecessor_cik, successor_cik, created_at, effective_date"
            ") VALUES (?, ?, ?, ?, ?)",
            (1, "0000000001", "0000000002", _NOW, "2025-01-01"),
        )
        connection.executemany(
            "INSERT INTO filings("
            "id, company_id, accession, form, filing_date, report_date, primary_document, "
            "source_url) VALUES (?, ?, ?, '10-K', '2026-01-01', '2025-12-31', "
            "'filing.htm', 'https://www.sec.gov/filing.htm')",
            (
                (11, 1, "0000000001-26-000001"),
                (12, 1, "0000000002-26-000001"),
                (13, 2, "0000000003-26-000001"),
            ),
        )
        connection.executemany(
            "INSERT INTO filing_provenance("
            "filing_id, company_id, registrant_cik, archive_owner_cik, recorded_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                (11, 1, "0000000001", "0000000001", _NOW),
                (12, 1, "0000000002", "0000000002", _NOW),
                (13, 2, "0000000003", "0000000003", _NOW),
            ),
        )
        connection.executemany(
            "INSERT INTO processing_runs("
            "id, company_id, purpose, requested_window_json, owner_pid, "
            "owner_process_start_identity, started_at, completed_at, status"
            ") VALUES (?, ?, 'test', ?, 1, 'test-owner', ?, ?, 'succeeded')",
            ((21, 1, "x" * 2_000_000, _NOW, _NOW), (22, 2, "{}", _NOW, _NOW)),
        )
        connection.executemany(
            "INSERT INTO processing_run_filings("
            "id, run_id, filing_id, status, started_at, completed_at"
            ") VALUES (?, ?, ?, 'reused', ?, ?)",
            ((31, 21, 11, _NOW, _NOW), (32, 21, 12, _NOW, _NOW), (33, 22, 13, _NOW, _NOW)),
        )
        connection.executemany(
            "INSERT INTO artifacts("
            "id, sha256, relative_object_path, byte_size, media_type, created_at"
            ") VALUES (?, ?, ?, ?, 'application/octet-stream', ?)",
            (
                (41, predecessor_hash, predecessor_path, predecessor_size, _NOW),
                (42, successor_hash, successor_path, successor_size, _NOW),
                (43, shared_hash, shared_path, shared_size, _NOW),
            ),
        )
        connection.executemany(
            "INSERT INTO attempt_artifacts("
            "attempt_id, artifact_id, purpose, logical_name"
            ") VALUES (?, ?, ?, ?)",
            (
                (31, 41, "filing", "predecessor.htm"),
                (32, 42, "filing", "successor.htm"),
                (31, 43, "taxonomy", "shared.xsd"),
                (33, 43, "taxonomy", "shared.xsd"),
            ),
        )

    for attempt_id in (31, 32, 33):
        staging = store.artifacts.stage_directory(str(attempt_id))
        (staging / "generated.tmp").write_bytes(b"temporary generated data")

    purger = CompanyDataPurger(store)
    plan = purger.preview(("LINE",))

    assert plan.artifact_file_count == 2
    assert plan.shared_artifact_count == 1
    assert plan.staging_directory_count == 2

    with store.database.connection() as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    database_size_before_purge = store.database.path.stat().st_size

    result = purger.purge(("LINE",))

    assert store.get_company_state("LINE") is None
    assert store.get_company_state_by_cik("0000000001") is None
    assert store.get_company_state_by_cik("0000000002") is None
    assert store.get_company_state("OTHER") is not None
    assert not (tmp_path / predecessor_path).exists()
    assert not (tmp_path / successor_path).exists()
    assert not (tmp_path / "staging" / "31").exists()
    assert not (tmp_path / "staging" / "32").exists()
    assert store.artifacts.resolve(shared_path, shared_hash, shared_size).is_file()
    assert (tmp_path / "staging" / "33" / "generated.tmp").is_file()
    assert store.database.path.stat().st_size < database_size_before_purge
    assert result.cleanup.failures == ()
