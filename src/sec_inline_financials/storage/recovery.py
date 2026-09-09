from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sec_inline_financials.errors import EvidenceStorageError
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.fingerprints import canonical_json


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _process_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def recover_interrupted_attempts(
    store: EvidenceStore,
    *,
    process_is_alive: Callable[[int], bool] = _process_is_alive,
) -> tuple[int, ...]:
    """Mark abandoned attempts interrupted while deferring every live owner."""
    with store.database.connection() as connection:
        abandoned = [
            (int(row["attempt_id"]), int(row["run_id"]))
            for row in connection.execute(
                "SELECT prf.id AS attempt_id, pr.id AS run_id, pr.owner_pid "
                "FROM processing_run_filings AS prf "
                "JOIN processing_runs AS pr ON pr.id = prf.run_id "
                "WHERE pr.status = 'running' AND prf.status IN ('pending', 'processing')"
            )
            if not process_is_alive(int(row["owner_pid"]))
        ]
    if not abandoned:
        return ()
    attempt_ids = [attempt_id for attempt_id, _run_id in abandoned]
    run_ids = sorted({run_id for _attempt_id, run_id in abandoned})
    with store.database.write_transaction() as connection:
        for attempt_id in attempt_ids:
            connection.execute(
                "UPDATE processing_run_filings SET status = 'interrupted', completed_at = ?, "
                "error_code = 'PROCESS_INTERRUPTED', "
                "error_text = 'Owning process is no longer running' "
                "WHERE id = ? AND status IN ('pending', 'processing')",
                (_now(), attempt_id),
            )
        for run_id in run_ids:
            remaining = connection.execute(
                "SELECT count(*) FROM processing_run_filings WHERE run_id = ? "
                "AND status IN ('pending', 'processing')",
                (run_id,),
            ).fetchone()[0]
            if int(remaining) == 0:
                connection.execute(
                    "UPDATE processing_runs SET status = 'interrupted', completed_at = ?, "
                    "error_summary = 'Owning process is no longer running' "
                    "WHERE id = ? AND status = 'running'",
                    (_now(), run_id),
                )
    return tuple(attempt_ids)


def backup_evidence(store: EvidenceStore, destination_root: Path) -> Path:
    """Create a quiescent SQLite backup plus every referenced immutable artifact."""
    if destination_root.exists():
        raise EvidenceStorageError(f"Backup destination already exists: {destination_root}")
    destination_root.mkdir(parents=True)
    destination_database = destination_root / "evidence.sqlite3"
    with store.database.connection() as source:
        running = source.execute(
            "SELECT count(*) FROM processing_runs WHERE status = 'running'"
        ).fetchone()[0]
        if int(running):
            raise EvidenceStorageError("Evidence backup requires quiescent ingestion.")
        artifact_rows = source.execute(
            "SELECT DISTINCT a.id, a.sha256, a.relative_object_path, a.byte_size, a.media_type "
            "FROM artifacts AS a WHERE EXISTS ("
            "SELECT 1 FROM snapshot_artifacts AS sa WHERE sa.artifact_id = a.id"
            ") OR EXISTS ("
            "SELECT 1 FROM attempt_artifacts AS aa WHERE aa.artifact_id = a.id"
            ") ORDER BY a.id"
        ).fetchall()
        with sqlite3.connect(destination_database) as destination:
            source.backup(destination)
    artifact_manifest: list[dict[str, object]] = []
    for row in artifact_rows:
        source_path = store.artifacts.resolve(
            str(row["relative_object_path"]),
            str(row["sha256"]),
            int(row["byte_size"]),
        )
        destination_path = destination_root / str(row["relative_object_path"])
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with source_path.open("rb") as input_stream, destination_path.open("xb") as output:
                while chunk := input_stream.read(1024 * 1024):
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            raise EvidenceStorageError(
                f"Could not copy backup artifact {source_path}: {exc}"
            ) from exc
        artifact_manifest.append(dict(row))
    database_hash = _sha256(destination_database)
    manifest_path = destination_root / "backup-manifest.json"
    manifest = canonical_json(
        {
            "version": "evidence-backup-v1",
            "database": {"path": "evidence.sqlite3", "sha256": database_hash},
            "artifacts": artifact_manifest,
        }
    )
    try:
        with manifest_path.open("x", encoding="utf-8", newline="\n") as stream:
            stream.write(manifest)
            stream.flush()
            os.fsync(stream.fileno())
    except OSError as exc:
        raise EvidenceStorageError(f"Could not write backup manifest: {exc}") from exc
    return manifest_path


def audit_backup(backup_root: Path) -> dict[str, object]:
    manifest_path = backup_root / "backup-manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise EvidenceStorageError(f"Could not read backup manifest: {exc}") from exc
    database_path = backup_root / "evidence.sqlite3"
    if _sha256(database_path) != manifest["database"]["sha256"]:
        raise EvidenceStorageError("Backup database hash does not match its manifest.")
    restored = EvidenceStore(database_path, backup_root)
    restored.initialize()
    with restored.database.connection() as connection:
        snapshot_ids = tuple(
            int(row["id"])
            for row in connection.execute("SELECT id FROM evidence_snapshots ORDER BY id")
        )
    audits = tuple(restored.audit_snapshot(snapshot_id) for snapshot_id in snapshot_ids)
    return {
        "database_sha256": manifest["database"]["sha256"],
        "snapshot_audits": audits,
        "ok": all(bool(audit["ok"]) for audit in audits),
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
    except OSError as exc:
        raise EvidenceStorageError(f"Could not hash backup file {path}: {exc}") from exc
    return digest.hexdigest()
