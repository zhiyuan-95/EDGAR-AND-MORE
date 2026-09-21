from __future__ import annotations

import re
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sec_inline_financials.errors import ArtifactError, CompanyPurgeError
from sec_inline_financials.storage.evidence_store import EvidenceStore

_TICKER_PATTERN = re.compile(r"[A-Z0-9.-]{1,10}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True)
class CompanyPurgeTarget:
    ticker: str
    cik: str
    name: str


@dataclass(frozen=True)
class CompanyPurgePlan:
    targets: tuple[CompanyPurgeTarget, ...]
    filing_count: int
    snapshot_count: int
    processing_run_count: int
    filing_attempt_count: int
    artifact_file_count: int
    artifact_bytes: int
    shared_artifact_count: int
    staging_directory_count: int
    active_ingestion_count: int
    pending_file_count: int


@dataclass(frozen=True)
class FileCleanupFailure:
    relative_path: str
    error: str


@dataclass(frozen=True)
class FileCleanupReport:
    deleted_paths: tuple[str, ...]
    missing_paths: tuple[str, ...]
    retained_paths: tuple[str, ...]
    failures: tuple[FileCleanupFailure, ...]


@dataclass(frozen=True)
class CompanyPurgeResult:
    plan: CompanyPurgePlan
    cleanup: FileCleanupReport


@dataclass(frozen=True)
class _ArtifactCandidate:
    relative_path: str
    byte_size: int
    is_shared: bool


class CompanyDataPurger:
    """Plan and execute an all-or-nothing company database purge plus queued file cleanup."""

    def __init__(self, store: EvidenceStore) -> None:
        self._store = store

    def preview(self, tickers: Sequence[str]) -> CompanyPurgePlan:
        requested = _normalize_tickers(tickers)
        self._store.initialize()
        with self._store.database.connection() as connection:
            targets = _load_targets(connection, requested)
            _create_scope(connection, tuple(target[0] for target in targets))
            return self._build_plan(connection, targets)

    def purge(self, tickers: Sequence[str]) -> CompanyPurgeResult:
        requested = _normalize_tickers(tickers)
        self._store.initialize()
        with self._store.database.write_transaction() as connection:
            active_ingestion_count = _count(
                connection, "SELECT count(*) FROM processing_runs WHERE status = 'running'"
            )
            if active_ingestion_count:
                raise CompanyPurgeError(
                    "Company purge requires quiescent ingestion; "
                    f"{active_ingestion_count} processing run(s) are still marked running."
                )
            targets = _load_targets(connection, requested)
            _create_scope(connection, tuple(target[0] for target in targets))
            plan = self._build_plan(connection, targets)
            connection.execute("PRAGMA defer_foreign_keys = ON")
            _queue_staging_directories(connection)
            _delete_target_rows(connection)
            _queue_unreferenced_artifacts(connection)
            _delete_unreferenced_artifact_records(connection)
            _delete_unreferenced_concepts(connection)
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                table = str(violations[0][0])
                raise CompanyPurgeError(
                    f"Company purge would leave foreign-key violations in {table}; no data changed."
                )
        return CompanyPurgeResult(plan=plan, cleanup=self.cleanup_pending_files())

    def cleanup_pending_files(self) -> FileCleanupReport:
        """Retry committed file deletions without racing an active ingestion."""
        self._store.initialize()
        deleted: list[str] = []
        missing: list[str] = []
        retained: list[str] = []
        failures: list[FileCleanupFailure] = []
        with self._store.database.write_transaction() as connection:
            rows = connection.execute(
                "SELECT relative_path, path_kind FROM pending_file_deletions "
                "ORDER BY path_kind, relative_path"
            ).fetchall()
            active_ingestion_count = _count(
                connection, "SELECT count(*) FROM processing_runs WHERE status = 'running'"
            )
            if active_ingestion_count:
                error = (
                    "Cleanup deferred because "
                    f"{active_ingestion_count} processing run(s) are still marked running."
                )
                return FileCleanupReport(
                    deleted_paths=(),
                    missing_paths=(),
                    retained_paths=(),
                    failures=tuple(
                        FileCleanupFailure(relative_path=str(row["relative_path"]), error=error)
                        for row in rows
                    ),
                )
            for row in rows:
                relative_path = str(row["relative_path"])
                path_kind = str(row["path_kind"])
                try:
                    outcome = self._cleanup_one(connection, relative_path, path_kind)
                except (ArtifactError, OSError, ValueError) as exc:
                    failures.append(
                        FileCleanupFailure(
                            relative_path=relative_path,
                            error=f"{type(exc).__name__}: {exc}",
                        )
                    )
                    continue
                connection.execute(
                    "DELETE FROM pending_file_deletions WHERE relative_path = ?",
                    (relative_path,),
                )
                if outcome == "deleted":
                    deleted.append(relative_path)
                elif outcome == "missing":
                    missing.append(relative_path)
                else:
                    retained.append(relative_path)
        return FileCleanupReport(
            deleted_paths=tuple(deleted),
            missing_paths=tuple(missing),
            retained_paths=tuple(retained),
            failures=tuple(failures),
        )

    def _build_plan(
        self,
        connection: sqlite3.Connection,
        target_rows: tuple[tuple[int, CompanyPurgeTarget], ...],
    ) -> CompanyPurgePlan:
        candidates = _artifact_candidates(connection)
        attempt_ids = tuple(
            int(row["id"])
            for row in connection.execute("SELECT id FROM purge_attempt_ids ORDER BY id")
        )
        return CompanyPurgePlan(
            targets=tuple(target for _company_id, target in target_rows),
            filing_count=_temp_count(connection, "purge_filing_ids"),
            snapshot_count=_temp_count(connection, "purge_snapshot_ids"),
            processing_run_count=_temp_count(connection, "purge_run_ids"),
            filing_attempt_count=len(attempt_ids),
            artifact_file_count=sum(not item.is_shared for item in candidates),
            artifact_bytes=sum(item.byte_size for item in candidates if not item.is_shared),
            shared_artifact_count=sum(item.is_shared for item in candidates),
            staging_directory_count=sum(
                (self._store.artifacts.staging_root / str(attempt_id)).exists()
                for attempt_id in attempt_ids
            ),
            active_ingestion_count=_count(
                connection, "SELECT count(*) FROM processing_runs WHERE status = 'running'"
            ),
            pending_file_count=_count(connection, "SELECT count(*) FROM pending_file_deletions"),
        )

    def _cleanup_one(
        self, connection: sqlite3.Connection, relative_path: str, path_kind: str
    ) -> str:
        if path_kind == "artifact":
            referenced = connection.execute(
                "SELECT 1 FROM artifacts WHERE relative_object_path = ?", (relative_path,)
            ).fetchone()
            if referenced is not None:
                return "retained"
            return "deleted" if self._store.artifacts.remove_object(relative_path) else "missing"
        if path_kind != "staging_directory":
            raise ValueError(f"Unsupported pending file kind: {path_kind}")
        path = PurePosixPath(relative_path)
        if len(path.parts) != 2 or path.parts[0] != "staging" or not path.parts[1].isdigit():
            raise ValueError(f"Unsafe pending staging path: {relative_path}")
        attempt_id = int(path.parts[1])
        reused = connection.execute(
            "SELECT 1 FROM processing_run_filings WHERE id = ?", (attempt_id,)
        ).fetchone()
        if reused is not None:
            return "retained"
        target = self._store.artifacts.staging_root / str(attempt_id)
        existed = target.exists()
        self._store.artifacts.cleanup_staging(str(attempt_id))
        return "deleted" if existed else "missing"


def _normalize_tickers(values: Sequence[str]) -> tuple[str, ...]:
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        for part in value.split(","):
            ticker = part.strip().upper()
            if not ticker:
                continue
            if _TICKER_PATTERN.fullmatch(ticker) is None:
                raise CompanyPurgeError(
                    f"Invalid ticker {ticker!r}; use 1-10 letters, digits, dots, or hyphens."
                )
            if ticker not in seen:
                normalized.append(ticker)
                seen.add(ticker)
    if not normalized:
        raise CompanyPurgeError("Provide at least one company ticker.")
    return tuple(normalized)


def _load_targets(
    connection: sqlite3.Connection, requested: tuple[str, ...]
) -> tuple[tuple[int, CompanyPurgeTarget], ...]:
    placeholders = ", ".join("?" for _value in requested)
    rows = connection.execute(
        "SELECT id, cik, ticker, current_name FROM companies "
        f"WHERE upper(ticker) IN ({placeholders})",
        requested,
    ).fetchall()
    by_ticker = {str(row["ticker"]).upper(): row for row in rows}
    missing = tuple(ticker for ticker in requested if ticker not in by_ticker)
    if missing:
        raise CompanyPurgeError(
            "No stored company data found for: " + ", ".join(missing) + ". No data changed."
        )
    return tuple(
        (
            int(by_ticker[ticker]["id"]),
            CompanyPurgeTarget(
                ticker=ticker,
                cik=str(by_ticker[ticker]["cik"]),
                name=str(by_ticker[ticker]["current_name"]),
            ),
        )
        for ticker in requested
    )


def _create_scope(connection: sqlite3.Connection, company_ids: tuple[int, ...]) -> None:
    connection.execute("CREATE TEMP TABLE purge_company_ids(id INTEGER PRIMARY KEY)")
    connection.executemany(
        "INSERT INTO purge_company_ids(id) VALUES (?)", ((value,) for value in company_ids)
    )
    statements = (
        "CREATE TEMP TABLE purge_filing_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_filing_ids SELECT id FROM filings "
        "WHERE company_id IN (SELECT id FROM purge_company_ids)",
        "CREATE TEMP TABLE purge_run_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_run_ids SELECT id FROM processing_runs "
        "WHERE company_id IN (SELECT id FROM purge_company_ids)",
        "CREATE TEMP TABLE purge_attempt_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_attempt_ids SELECT id FROM processing_run_filings "
        "WHERE run_id IN (SELECT id FROM purge_run_ids) "
        "OR filing_id IN (SELECT id FROM purge_filing_ids)",
        "CREATE TEMP TABLE purge_snapshot_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_snapshot_ids SELECT id FROM evidence_snapshots "
        "WHERE filing_id IN (SELECT id FROM purge_filing_ids)",
        "CREATE TEMP TABLE purge_report_evaluation_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_report_evaluation_ids SELECT id FROM report_evaluations "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "CREATE TEMP TABLE purge_metric_evaluation_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_metric_evaluation_ids SELECT id FROM metric_evaluations "
        "WHERE company_id IN (SELECT id FROM purge_company_ids)",
        "CREATE TEMP TABLE purge_metric_result_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_metric_result_ids SELECT id FROM metric_results "
        "WHERE evaluation_id IN (SELECT id FROM purge_metric_evaluation_ids) "
        "OR snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "CREATE TEMP TABLE purge_issue_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_issue_ids SELECT id FROM reconciliation_issues "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "CREATE TEMP TABLE purge_unit_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_unit_ids SELECT id FROM units "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "CREATE TEMP TABLE purge_concept_ids(id INTEGER PRIMARY KEY)",
        "INSERT INTO purge_concept_ids SELECT DISTINCT concept_id FROM snapshot_concepts "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "CREATE TEMP TABLE purge_artifact_ids(id INTEGER PRIMARY KEY)",
        "INSERT OR IGNORE INTO purge_artifact_ids SELECT artifact_id FROM snapshot_artifacts "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "INSERT OR IGNORE INTO purge_artifact_ids SELECT artifact_id FROM attempt_artifacts "
        "WHERE attempt_id IN (SELECT id FROM purge_attempt_ids)",
        "INSERT OR IGNORE INTO purge_artifact_ids SELECT artifact_id FROM source_documents "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids) AND artifact_id IS NOT NULL",
    )
    for statement in statements:
        connection.execute(statement)


def _artifact_candidates(connection: sqlite3.Connection) -> tuple[_ArtifactCandidate, ...]:
    rows = connection.execute(
        "SELECT a.relative_object_path, a.byte_size, "
        "EXISTS(SELECT 1 FROM snapshot_artifacts AS sa "
        " WHERE sa.artifact_id = a.id "
        " AND sa.snapshot_id NOT IN (SELECT id FROM purge_snapshot_ids)) "
        "OR EXISTS(SELECT 1 FROM attempt_artifacts AS aa "
        " WHERE aa.artifact_id = a.id "
        " AND aa.attempt_id NOT IN (SELECT id FROM purge_attempt_ids)) "
        "OR EXISTS(SELECT 1 FROM source_documents AS sd "
        " WHERE sd.artifact_id = a.id "
        " AND sd.snapshot_id NOT IN (SELECT id FROM purge_snapshot_ids)) AS is_shared "
        "FROM artifacts AS a JOIN purge_artifact_ids AS p ON p.id = a.id ORDER BY a.id"
    ).fetchall()
    return tuple(
        _ArtifactCandidate(
            relative_path=str(row["relative_object_path"]),
            byte_size=int(row["byte_size"]),
            is_shared=bool(row["is_shared"]),
        )
        for row in rows
    )


def _queue_staging_directories(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO pending_file_deletions(relative_path, path_kind, queued_at) "
        "SELECT 'staging/' || id, 'staging_directory', ? FROM purge_attempt_ids",
        (_now(),),
    )


def _delete_target_rows(connection: sqlite3.Connection) -> None:
    statements = (
        "DELETE FROM published_metric_evaluations "
        "WHERE company_id IN (SELECT id FROM purge_company_ids)",
        "DELETE FROM metric_result_facts "
        "WHERE metric_result_id IN (SELECT id FROM purge_metric_result_ids)",
        "DELETE FROM metric_results WHERE id IN (SELECT id FROM purge_metric_result_ids)",
        "DELETE FROM metric_evaluations WHERE id IN (SELECT id FROM purge_metric_evaluation_ids)",
        "DELETE FROM active_filing_snapshots WHERE filing_id IN (SELECT id FROM purge_filing_ids)",
        "DELETE FROM reconciliation_issue_facts WHERE issue_id IN (SELECT id FROM purge_issue_ids)",
        "DELETE FROM reconciliation_issues WHERE id IN (SELECT id FROM purge_issue_ids)",
        "DELETE FROM fact_exclusion_reasons "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM fact_report_status WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM report_evaluations WHERE id IN (SELECT id FROM purge_report_evaluation_ids)",
        "DELETE FROM validation_references "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM validation_messages WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM extraction_diagnostics "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM calculation_arcs WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM relationship_networks "
        "WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM roles WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM filing_sections WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM facts WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM context_dimensions WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM unit_measures WHERE unit_id IN (SELECT id FROM purge_unit_ids)",
        "DELETE FROM units WHERE id IN (SELECT id FROM purge_unit_ids)",
        "DELETE FROM contexts WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM concept_labels WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM snapshot_concepts WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM source_documents WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM snapshot_artifacts WHERE snapshot_id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM attempt_artifacts WHERE attempt_id IN (SELECT id FROM purge_attempt_ids)",
        "DELETE FROM evidence_snapshots WHERE id IN (SELECT id FROM purge_snapshot_ids)",
        "DELETE FROM processing_run_filings WHERE id IN (SELECT id FROM purge_attempt_ids)",
        "DELETE FROM processing_runs WHERE id IN (SELECT id FROM purge_run_ids)",
        "DELETE FROM filings WHERE id IN (SELECT id FROM purge_filing_ids)",
        "DELETE FROM companies WHERE id IN (SELECT id FROM purge_company_ids)",
    )
    for statement in statements:
        connection.execute(statement)


def _queue_unreferenced_artifacts(connection: sqlite3.Connection) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO pending_file_deletions(relative_path, path_kind, queued_at) "
        "SELECT a.relative_object_path, 'artifact', ? FROM artifacts AS a "
        "JOIN purge_artifact_ids AS p ON p.id = a.id "
        "WHERE NOT EXISTS(SELECT 1 FROM snapshot_artifacts WHERE artifact_id = a.id) "
        "AND NOT EXISTS(SELECT 1 FROM attempt_artifacts WHERE artifact_id = a.id) "
        "AND NOT EXISTS(SELECT 1 FROM source_documents WHERE artifact_id = a.id)",
        (_now(),),
    )


def _delete_unreferenced_artifact_records(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DELETE FROM artifacts WHERE id IN (SELECT id FROM purge_artifact_ids) "
        "AND NOT EXISTS(SELECT 1 FROM snapshot_artifacts WHERE artifact_id = artifacts.id) "
        "AND NOT EXISTS(SELECT 1 FROM attempt_artifacts WHERE artifact_id = artifacts.id) "
        "AND NOT EXISTS(SELECT 1 FROM source_documents WHERE artifact_id = artifacts.id)"
    )


def _delete_unreferenced_concepts(connection: sqlite3.Connection) -> None:
    connection.execute(
        "DELETE FROM concepts WHERE id IN (SELECT id FROM purge_concept_ids) "
        "AND NOT EXISTS(SELECT 1 FROM snapshot_concepts WHERE concept_id = concepts.id)"
    )


def _temp_count(connection: sqlite3.Connection, table: str) -> int:
    if not table.startswith("purge_"):
        raise ValueError(f"Unsafe temporary table name: {table}")
    return _count(connection, f"SELECT count(*) FROM {table}")


def _count(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    if row is None:
        raise CompanyPurgeError("Could not count company purge records.")
    return int(row[0])
