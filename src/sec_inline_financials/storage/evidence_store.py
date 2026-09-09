from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from sec_inline_financials.errors import (
    FilingMetadataError,
    SnapshotMismatchError,
)
from sec_inline_financials.evidence_models import (
    CalculationNetworkRecord,
    CalculationRelationshipRecord,
    ConceptLabelRecord,
    ConceptRecord,
    ContextDimensionRecord,
    ContextRecord,
    CoverageManifest,
    ExtractionDiagnosticRecord,
    ExtractionProfile,
    FactQuery,
    FactReportStatus,
    FilingEvidenceBundle,
    InstalledArtifact,
    ObservationRecord,
    Page,
    ReconciliationIssueRecord,
    ReportEvaluation,
    ReportEvaluationRef,
    SnapshotRef,
    SourceDocumentRecord,
    StoreResult,
    UnitMeasureRecord,
    UnitRecord,
    ValidationRecord,
    ValidationReferenceRecord,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage.artifacts import ArtifactStore
from sec_inline_financials.storage.database import EvidenceDatabase
from sec_inline_financials.storage.fingerprints import (
    canonical_json,
    extraction_profile_hash,
    extraction_profile_json,
    payload_hash,
    source_manifest_hash,
)
from sec_inline_financials.storage.migrations import initialize_database

_PROCESS_START_IDENTITY = f"{os.getpid()}:{time.time_ns()}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _optional_date(value: object) -> date | None:
    return date.fromisoformat(str(value)) if value is not None else None


def _bool(value: object) -> bool | None:
    return None if value is None else bool(value)


class EvidenceStore:
    """Deep module for transactional evidence persistence and bounded retrieval."""

    def __init__(
        self,
        database_path: Path,
        artifact_root: Path | None = None,
        *,
        busy_timeout_seconds: float = 5.0,
    ) -> None:
        self.database = EvidenceDatabase(database_path, busy_timeout_seconds=busy_timeout_seconds)
        self.artifacts = ArtifactStore(artifact_root or database_path.parent)

    def initialize(self) -> int:
        return initialize_database(self.database)

    def create_processing_run(
        self,
        company: Company,
        *,
        purpose: str,
        requested_window: dict[str, object],
    ) -> int:
        with self.database.write_transaction() as connection:
            company_id = self._upsert_company(connection, company)
            cursor = connection.execute(
                "INSERT INTO processing_runs("
                "company_id, purpose, requested_window_json, owner_pid, "
                "owner_process_start_identity, started_at, status"
                ") VALUES (?, ?, ?, ?, ?, ?, 'running')",
                (
                    company_id,
                    purpose,
                    canonical_json(requested_window),
                    os.getpid(),
                    _PROCESS_START_IDENTITY,
                    _now(),
                ),
            )
            return _last_row_id(cursor)

    def begin_filing_attempt(self, run_id: int, company: Company, filing: Filing) -> int:
        with self.database.write_transaction() as connection:
            company_id = self._upsert_company(connection, company)
            run = connection.execute(
                "SELECT company_id, status FROM processing_runs WHERE id = ?", (run_id,)
            ).fetchone()
            if run is None or int(run["company_id"]) != company_id:
                raise FilingMetadataError("Processing run does not belong to this company.")
            if str(run["status"]) != "running":
                raise FilingMetadataError("Cannot add a filing to a completed processing run.")
            filing_id = self._upsert_filing(connection, company_id, filing)
            existing = connection.execute(
                "SELECT id, status FROM processing_run_filings WHERE run_id = ? AND filing_id = ?",
                (run_id, filing_id),
            ).fetchone()
            if existing is not None:
                if str(existing["status"]) in {"pending", "processing"}:
                    return int(existing["id"])
                raise FilingMetadataError("This run already completed the filing attempt.")
            cursor = connection.execute(
                "INSERT INTO processing_run_filings("
                "run_id, filing_id, status, started_at"
                ") VALUES (?, ?, 'processing', ?)",
                (run_id, filing_id, _now()),
            )
            return _last_row_id(cursor)

    def mark_attempt_failed(self, attempt_id: int, error: BaseException) -> None:
        with self.database.write_transaction() as connection:
            connection.execute(
                "UPDATE processing_run_filings SET status = 'failed', completed_at = ?, "
                "error_code = ?, error_text = ? WHERE id = ? "
                "AND status IN ('pending', 'processing')",
                (_now(), type(error).__name__, str(error), attempt_id),
            )

    def register_attempt_artifacts(
        self, attempt_id: int, artifacts: tuple[InstalledArtifact, ...]
    ) -> None:
        """Retain operational artifacts even when no evidence snapshot can commit."""
        with self.database.write_transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM processing_run_filings WHERE id = ?", (attempt_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"Unknown processing attempt {attempt_id}.")
            artifact_ids = self._register_artifacts(connection, artifacts)
            self._link_attempt_artifacts(connection, attempt_id, artifacts, artifact_ids)

    def mark_attempt_reused(self, attempt_id: int, snapshot_id: int) -> None:
        with self.database.write_transaction() as connection:
            row = connection.execute(
                "SELECT prf.filing_id AS attempt_filing_id, s.filing_id AS snapshot_filing_id "
                "FROM processing_run_filings AS prf JOIN evidence_snapshots AS s ON s.id = ? "
                "WHERE prf.id = ?",
                (snapshot_id, attempt_id),
            ).fetchone()
            if row is None or int(row["attempt_filing_id"]) != int(row["snapshot_filing_id"]):
                raise FilingMetadataError("Reusable snapshot does not belong to this attempt.")
            connection.execute(
                "UPDATE processing_run_filings SET status = 'reused', completed_at = ?, "
                "snapshot_id = ? WHERE id = ? AND status IN ('pending', 'processing')",
                (_now(), snapshot_id, attempt_id),
            )

    def finish_processing_run(self, run_id: int) -> Literal["succeeded", "partial", "failed"]:
        with self.database.write_transaction() as connection:
            statuses = [
                str(row["status"])
                for row in connection.execute(
                    "SELECT status FROM processing_run_filings WHERE run_id = ?", (run_id,)
                )
            ]
            successes = sum(status in {"stored", "reused"} for status in statuses)
            failures = sum(status in {"failed", "interrupted"} for status in statuses)
            if successes and failures:
                status: Literal["succeeded", "partial", "failed"] = "partial"
            elif successes and not failures:
                status = "succeeded"
            else:
                status = "failed"
            connection.execute(
                "UPDATE processing_runs SET status = ?, completed_at = ? WHERE id = ?",
                (status, _now(), run_id),
            )
            return status

    def find_reusable_snapshot(
        self, accession: str, extraction_profile_hash_value: str
    ) -> SnapshotRef | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT s.id, s.filing_id, f.accession, s.source_manifest_hash, "
                "s.extraction_profile_hash, s.payload_hash "
                "FROM evidence_snapshots AS s JOIN filings AS f ON f.id = s.filing_id "
                "WHERE f.accession = ? AND s.extraction_profile_hash = ? "
                "ORDER BY s.id DESC LIMIT 1",
                (accession, extraction_profile_hash_value),
            ).fetchone()
            if row is None:
                return None
            self._verify_snapshot_artifacts(connection, int(row["id"]))
            return self._snapshot_ref(row)

    def find_committed_attempt(
        self, attempt_id: int, bundle: FilingEvidenceBundle
    ) -> StoreResult | None:
        """Resolve an uncertain return by reading the attempt after reconnecting."""
        manifest_hash = source_manifest_hash(bundle)
        profile_hash = extraction_profile_hash(bundle)
        semantic_hash = payload_hash(bundle)
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT prf.status, prf.snapshot_id, s.payload_hash "
                "FROM processing_run_filings AS prf "
                "LEFT JOIN evidence_snapshots AS s ON s.id = prf.snapshot_id "
                "JOIN filings AS f ON f.id = prf.filing_id "
                "WHERE prf.id = ? AND f.accession = ? AND s.source_manifest_hash = ? "
                "AND s.extraction_profile_hash = ?",
                (attempt_id, bundle.filing.accession, manifest_hash, profile_hash),
            ).fetchone()
            if row is None or row["snapshot_id"] is None:
                return None
            if str(row["payload_hash"]) != semantic_hash:
                raise SnapshotMismatchError(
                    "Committed attempt has a different semantic payload hash."
                )
            status = str(row["status"])
            if status not in {"stored", "reused"}:
                return None
            return StoreResult(
                snapshot_id=int(row["snapshot_id"]),
                disposition=status,  # type: ignore[arg-type]
            )

    def save_snapshot(
        self,
        bundle: FilingEvidenceBundle,
        evaluation: ReportEvaluation,
        attempt_id: int,
        installed_artifacts: tuple[InstalledArtifact, ...],
    ) -> StoreResult:
        self._validate_bundle(bundle, evaluation)
        manifest_hash = source_manifest_hash(bundle)
        profile_hash = extraction_profile_hash(bundle)
        semantic_hash = payload_hash(bundle)
        with self.database.write_transaction() as connection:
            company_id = self._upsert_company(connection, bundle.company)
            filing_id = self._upsert_filing(connection, company_id, bundle.filing)
            attempt = connection.execute(
                "SELECT filing_id, status FROM processing_run_filings WHERE id = ?",
                (attempt_id,),
            ).fetchone()
            if attempt is None or int(attempt["filing_id"]) != filing_id:
                raise FilingMetadataError("Snapshot attempt does not belong to this filing.")
            if str(attempt["status"]) not in {"pending", "processing"}:
                raise FilingMetadataError("Snapshot attempt is already complete.")

            artifact_ids = self._register_artifacts(connection, installed_artifacts)
            self._link_attempt_artifacts(connection, attempt_id, installed_artifacts, artifact_ids)
            existing = connection.execute(
                "SELECT id, filing_id, source_manifest_hash, extraction_profile_hash, payload_hash "
                "FROM evidence_snapshots WHERE filing_id = ? AND source_manifest_hash = ? "
                "AND extraction_profile_hash = ?",
                (filing_id, manifest_hash, profile_hash),
            ).fetchone()
            if existing is not None:
                if str(existing["payload_hash"]) != semantic_hash:
                    raise SnapshotMismatchError(
                        "Existing snapshot identity has a different semantic payload hash."
                    )
                snapshot_id = int(existing["id"])
                connection.execute(
                    "UPDATE processing_run_filings SET status = 'reused', completed_at = ?, "
                    "snapshot_id = ? WHERE id = ?",
                    (_now(), snapshot_id, attempt_id),
                )
                return StoreResult(snapshot_id=snapshot_id, disposition="reused")

            metadata = {
                "version": "filing-metadata-v1",
                "accession": bundle.filing.accession,
                "filing_date": bundle.filing.filing_date,
                "report_date": bundle.filing.report_date,
                "form": bundle.filing.form,
                "primary_document": bundle.filing.primary_document,
                "source_url": bundle.filing.url,
                "company_cik": bundle.company.cik,
            }
            cursor = connection.execute(
                "INSERT INTO evidence_snapshots("
                "filing_id, completed_by_attempt_id, captured_company_name, "
                "captured_company_ticker, captured_filing_metadata_json, fiscal_year, "
                "fiscal_period, fiscal_year_source, fiscal_period_source, "
                "source_manifest_hash, extraction_profile_hash, extraction_profile_json, "
                "payload_hash, coverage_manifest_json, captured_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    filing_id,
                    attempt_id,
                    bundle.captured_company_name,
                    bundle.captured_company_ticker,
                    canonical_json(metadata),
                    bundle.fiscal_year,
                    bundle.fiscal_period,
                    bundle.fiscal_year_source,
                    bundle.fiscal_period_source,
                    manifest_hash,
                    profile_hash,
                    extraction_profile_json(bundle),
                    semantic_hash,
                    canonical_json(bundle.coverage_manifest),
                    _now(),
                ),
            )
            snapshot_id = _last_row_id(cursor)
            maps = self._insert_snapshot_evidence(
                connection,
                snapshot_id,
                bundle,
                installed_artifacts,
                artifact_ids,
            )
            self._insert_evaluation(connection, snapshot_id, evaluation, maps)
            connection.execute(
                "UPDATE processing_run_filings SET status = 'stored', completed_at = ?, "
                "snapshot_id = ? WHERE id = ?",
                (_now(), snapshot_id, attempt_id),
            )
            return StoreResult(snapshot_id=snapshot_id, disposition="stored")

    def load_snapshot(self, snapshot_id: int) -> FilingEvidenceBundle:
        with self.database.connection() as connection:
            snapshot = connection.execute(
                "SELECT s.*, f.accession, f.form, f.filing_date, f.report_date, "
                "f.primary_document, f.source_url, c.cik "
                "FROM evidence_snapshots AS s "
                "JOIN filings AS f ON f.id = s.filing_id "
                "JOIN companies AS c ON c.id = f.company_id WHERE s.id = ?",
                (snapshot_id,),
            ).fetchone()
            if snapshot is None:
                raise KeyError(f"Unknown evidence snapshot {snapshot_id}.")
            source_documents = self._load_source_documents(connection, snapshot_id)
            concepts, labels = self._load_concepts(connection, snapshot_id)
            contexts = self._load_contexts(connection, snapshot_id)
            units = self._load_units(connection, snapshot_id)
            observations = self._load_observations(connection, snapshot_id)
            diagnostics = self._load_diagnostics(connection, snapshot_id)
            validation = self._load_validation(connection, snapshot_id)
            networks, relationships = self._load_calculations(connection, snapshot_id)
            raw_log = self._load_raw_log(connection, snapshot_id)
            profile_data = json.loads(str(snapshot["extraction_profile_json"]))
            coverage_data = json.loads(str(snapshot["coverage_manifest_json"]))
            ticker = str(snapshot["captured_company_ticker"] or "")
            company = Company(
                ticker=ticker,
                cik=str(snapshot["cik"]),
                name=str(snapshot["captured_company_name"]),
            )
            filing = Filing(
                accession=str(snapshot["accession"]),
                filing_date=date.fromisoformat(str(snapshot["filing_date"])),
                report_date=date.fromisoformat(str(snapshot["report_date"])),
                form=str(snapshot["form"]),
                primary_document=str(snapshot["primary_document"]),
                url=str(snapshot["source_url"]),
            )
            return FilingEvidenceBundle(
                company=company,
                filing=filing,
                captured_company_name=str(snapshot["captured_company_name"]),
                captured_company_ticker=(
                    str(snapshot["captured_company_ticker"])
                    if snapshot["captured_company_ticker"] is not None
                    else None
                ),
                fiscal_year=(int(snapshot["fiscal_year"]) if snapshot["fiscal_year"] else None),
                fiscal_period=(
                    str(snapshot["fiscal_period"])
                    if snapshot["fiscal_period"] is not None
                    else None
                ),
                fiscal_year_source=(
                    str(snapshot["fiscal_year_source"])
                    if snapshot["fiscal_year_source"] is not None
                    else None
                ),
                fiscal_period_source=(
                    str(snapshot["fiscal_period_source"])
                    if snapshot["fiscal_period_source"] is not None
                    else None
                ),
                source_documents=source_documents,
                concepts=concepts,
                concept_labels=labels,
                contexts=contexts,
                units=units,
                observations=observations,
                diagnostics=diagnostics,
                validation_messages=validation,
                calculation_networks=networks,
                calculation_relationships=relationships,
                extraction_profile=ExtractionProfile(
                    application_version=str(profile_data["application_version"]),
                    extractor_version=str(profile_data["extractor_version"]),
                    arelle_version=str(profile_data["arelle_version"]),
                    validation_options=tuple(
                        (str(item[0]), str(item[1])) for item in profile_data["validation_options"]
                    ),
                    transform_plugin_revision=str(profile_data["transform_plugin_revision"]),
                    transform_plugin_hashes=tuple(
                        (str(item[0]), str(item[1]))
                        for item in profile_data["transform_plugin_hashes"]
                    ),
                    serialization_version=str(profile_data["serialization_version"]),
                ),
                coverage_manifest=CoverageManifest(**coverage_data),
                raw_log_json=raw_log,
            )

    def get_report_evaluation(
        self, snapshot_id: int, report_kind: str, rule_version: str
    ) -> ReportEvaluationRef | None:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT id, snapshot_id, report_kind, rule_version FROM report_evaluations "
                "WHERE snapshot_id = ? AND report_kind = ? AND rule_version = ?",
                (snapshot_id, report_kind, rule_version),
            ).fetchone()
            if row is None:
                return None
            return ReportEvaluationRef(
                evaluation_id=int(row["id"]),
                snapshot_id=int(row["snapshot_id"]),
                report_kind=str(row["report_kind"]),  # type: ignore[arg-type]
                rule_version=str(row["rule_version"]),
            )

    def save_report_evaluation(
        self, snapshot_id: int, evaluation: ReportEvaluation
    ) -> ReportEvaluationRef:
        with self.database.write_transaction() as connection:
            existing = connection.execute(
                "SELECT id FROM report_evaluations WHERE snapshot_id = ? "
                "AND report_kind = ? AND rule_version = ?",
                (snapshot_id, evaluation.report_kind, evaluation.rule_version),
            ).fetchone()
            maps = self._existing_maps(connection, snapshot_id)
            if existing is not None:
                stored = self._load_evaluation(connection, int(existing["id"]))
                if canonical_json(stored) != canonical_json(evaluation):
                    raise SnapshotMismatchError(
                        "Report decisions changed without a new rule version."
                    )
                evaluation_id = int(existing["id"])
            else:
                evaluation_id = self._insert_evaluation(connection, snapshot_id, evaluation, maps)
            return ReportEvaluationRef(
                evaluation_id=evaluation_id,
                snapshot_id=snapshot_id,
                report_kind=evaluation.report_kind,
                rule_version=evaluation.rule_version,
            )

    def load_report_evaluation(self, evaluation_id: int) -> ReportEvaluation:
        with self.database.connection() as connection:
            return self._load_evaluation(connection, evaluation_id)

    def list_concepts(self, snapshot_id: int, cursor: int = 0, limit: int = 100) -> Page:
        limit = self._page_limit(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT c.id, c.namespace_uri, c.local_name, sc.display_qname, "
                "sc.definition_status, sc.period_type, sc.balance, sc.is_numeric "
                "FROM snapshot_concepts AS sc JOIN concepts AS c ON c.id = sc.concept_id "
                "WHERE sc.snapshot_id = ? AND c.id > ? ORDER BY c.id LIMIT ?",
                (snapshot_id, cursor, limit + 1),
            ).fetchall()
            return self._page(rows, limit)

    def list_facts(
        self,
        snapshot_id: int,
        filters: FactQuery | None = None,
        cursor: int = 0,
        limit: int = 100,
    ) -> Page:
        limit = self._page_limit(limit)
        filters = filters or FactQuery()
        clauses = ["f.snapshot_id = ?", "f.id > ?"]
        parameters: list[object] = [snapshot_id, cursor]
        if filters.concept_namespace_uri is not None:
            clauses.append("c.namespace_uri = ?")
            parameters.append(filters.concept_namespace_uri)
        if filters.concept_local_name is not None:
            clauses.append("c.local_name = ?")
            parameters.append(filters.concept_local_name)
        if filters.period_start_date is not None:
            clauses.append("ctx.period_start_date = ?")
            parameters.append(filters.period_start_date.isoformat())
        if filters.period_end_date is not None:
            clauses.append("ctx.period_end_date = ?")
            parameters.append(filters.period_end_date.isoformat())
        role_join = ""
        if filters.evidence_role is not None:
            if filters.evaluation_id is None:
                raise ValueError("An evaluation_id is required when filtering by evidence role.")
            role_join = (
                " JOIN fact_report_status AS frs ON frs.snapshot_id = f.snapshot_id "
                "AND frs.fact_id = f.id"
            )
            clauses.extend(["frs.evaluation_id = ?", "frs.evidence_role = ?"])
            parameters.extend([filters.evaluation_id, filters.evidence_role])
        parameters.append(limit + 1)
        sql = (
            "SELECT f.id, f.observation_key, f.source_order, f.display_qname, "
            "f.raw_value_text, f.typed_value_text, f.is_nil, f.is_numeric, "
            "f.validity_name, ctx.period_start_date, ctx.period_end_date, "
            "u.legacy_report_unit_text, c.namespace_uri, c.local_name "
            "FROM facts AS f LEFT JOIN concepts AS c ON c.id = f.concept_id "
            "LEFT JOIN contexts AS ctx ON ctx.id = f.context_id "
            "LEFT JOIN units AS u ON u.id = f.unit_id"
            f"{role_join} WHERE {' AND '.join(clauses)} ORDER BY f.id LIMIT ?"
        )
        with self.database.connection() as connection:
            rows = connection.execute(sql, parameters).fetchall()
            return self._page(rows, limit)

    def get_fact(self, fact_id: int) -> dict[str, object]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT f.*, c.namespace_uri, c.local_name, ctx.context_key, "
                "ctx.period_kind, ctx.period_start_date, ctx.period_end_date, "
                "u.unit_key, u.legacy_report_unit_text "
                "FROM facts AS f LEFT JOIN concepts AS c ON c.id = f.concept_id "
                "LEFT JOIN contexts AS ctx ON ctx.id = f.context_id "
                "LEFT JOIN units AS u ON u.id = f.unit_id WHERE f.id = ?",
                (fact_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown fact {fact_id}.")
            result = dict(row)
            result["dimensions"] = tuple(
                dict(item)
                for item in connection.execute(
                    "SELECT * FROM fact_dimensions WHERE fact_id = ? ORDER BY source_order",
                    (fact_id,),
                )
            )
            return result

    def get_conflict(self, issue_id: int) -> dict[str, object]:
        with self.database.connection() as connection:
            issue = connection.execute(
                "SELECT ri.*, c.namespace_uri, c.local_name FROM reconciliation_issues AS ri "
                "JOIN concepts AS c ON c.id = ri.concept_id WHERE ri.id = ?",
                (issue_id,),
            ).fetchone()
            if issue is None:
                raise KeyError(f"Unknown reconciliation issue {issue_id}.")
            result = dict(issue)
            result["candidates"] = tuple(
                dict(row)
                for row in connection.execute(
                    "SELECT rif.candidate_order, f.* FROM reconciliation_issue_facts AS rif "
                    "JOIN facts AS f ON f.id = rif.fact_id WHERE rif.issue_id = ? "
                    "ORDER BY rif.candidate_order",
                    (issue_id,),
                )
            )
            return result

    def list_calculation_children(
        self, snapshot_id: int, role_uri: str, parent_concept: str
    ) -> tuple[dict[str, object], ...]:
        namespace_uri, local_name = _split_concept_key(parent_concept)
        with self.database.connection() as connection:
            return tuple(
                dict(row)
                for row in connection.execute(
                    "SELECT ca.id, ca.relationship_order, ca.exact_weight_text, "
                    "ca.exact_order_text, child.namespace_uri AS child_namespace_uri, "
                    "child.local_name AS child_local_name "
                    "FROM calculation_arcs AS ca JOIN roles AS r ON r.id = ca.role_id "
                    "JOIN concepts AS parent ON parent.id = ca.parent_concept_id "
                    "JOIN concepts AS child ON child.id = ca.child_concept_id "
                    "WHERE ca.snapshot_id = ? AND r.role_uri = ? "
                    "AND parent.namespace_uri = ? AND parent.local_name = ? "
                    "ORDER BY ca.relationship_order, ca.id",
                    (snapshot_id, role_uri, namespace_uri, local_name),
                )
            )

    def list_validation_messages(self, snapshot_id: int, cursor: int = 0, limit: int = 100) -> Page:
        limit = self._page_limit(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                "SELECT id, message_order, level, code, message_text, raw_record_json "
                "FROM validation_messages WHERE snapshot_id = ? AND id > ? "
                "ORDER BY id LIMIT ?",
                (snapshot_id, cursor, limit + 1),
            ).fetchall()
            return self._page(rows, limit)

    def resolve_artifact(self, artifact_id: int) -> tuple[Path, dict[str, object]]:
        with self.database.connection() as connection:
            row = connection.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"Unknown artifact {artifact_id}.")
            path = self.artifacts.resolve(
                str(row["relative_object_path"]),
                str(row["sha256"]),
                int(row["byte_size"]),
            )
            return path, dict(row)

    def audit_snapshot(self, snapshot_id: int) -> dict[str, object]:
        with self.database.connection() as connection:
            exists = connection.execute(
                "SELECT 1 FROM evidence_snapshots WHERE id = ?", (snapshot_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"Unknown evidence snapshot {snapshot_id}.")
            counts = {
                table: int(
                    connection.execute(
                        f"SELECT count(*) FROM {table} WHERE snapshot_id = ?",  # noqa: S608
                        (snapshot_id,),
                    ).fetchone()[0]
                )
                for table in (
                    "source_documents",
                    "snapshot_concepts",
                    "contexts",
                    "units",
                    "facts",
                    "extraction_diagnostics",
                    "validation_messages",
                    "relationship_networks",
                    "calculation_arcs",
                    "report_evaluations",
                )
            }
            artifacts = self._verify_snapshot_artifacts(connection, snapshot_id)
            foreign_key_issues = tuple(
                dict(row) for row in connection.execute("PRAGMA foreign_key_check")
            )
            return {
                "snapshot_id": snapshot_id,
                "counts": counts,
                "artifacts": artifacts,
                "foreign_key_issues": foreign_key_issues,
                "ok": not foreign_key_issues,
            }

    @staticmethod
    def _page_limit(limit: int) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError("Page limit must be between 1 and 1000.")
        return limit

    @staticmethod
    def _page(rows: list[sqlite3.Row], limit: int) -> Page:
        visible = rows[:limit]
        return Page(
            items=tuple(dict(row) for row in visible),
            next_cursor=(int(visible[-1]["id"]) if len(rows) > limit and visible else None),
        )

    @staticmethod
    def _snapshot_ref(row: sqlite3.Row) -> SnapshotRef:
        return SnapshotRef(
            snapshot_id=int(row["id"]),
            filing_id=int(row["filing_id"]),
            accession=str(row["accession"]),
            source_manifest_hash=str(row["source_manifest_hash"]),
            extraction_profile_hash=str(row["extraction_profile_hash"]),
            payload_hash=str(row["payload_hash"]),
        )

    @staticmethod
    def _validate_bundle(bundle: FilingEvidenceBundle, evaluation: ReportEvaluation) -> None:
        if evaluation.report_date != bundle.filing.report_date:
            raise ValueError("Report evaluation date does not match the filing report date.")
        observation_keys = [observation.key for observation in bundle.observations]
        if len(observation_keys) != len(set(observation_keys)):
            raise ValueError("Observation keys must be unique within a snapshot.")
        if len(evaluation.statuses) != len(bundle.observations):
            raise ValueError("Every observation must have one report status.")
        if {status.fact_key for status in evaluation.statuses} != set(observation_keys):
            raise ValueError("Report status links do not cover the observation set.")
        expected = bundle.coverage_manifest.recognized_fact_count + (
            bundle.coverage_manifest.unresolved_observation_count
        )
        if expected != len(bundle.observations):
            raise ValueError("Coverage manifest does not match detached observations.")

    @staticmethod
    def _upsert_company(connection: sqlite3.Connection, company: Company) -> int:
        now = _now()
        row = connection.execute(
            "SELECT id FROM companies WHERE cik = ?", (company.cik,)
        ).fetchone()
        if row is None:
            cursor = connection.execute(
                "INSERT INTO companies(cik, ticker, current_name, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (company.cik, company.ticker or None, company.name, now, now),
            )
            return _last_row_id(cursor)
        company_id = int(row["id"])
        connection.execute(
            "UPDATE companies SET ticker = ?, current_name = ?, updated_at = ? WHERE id = ?",
            (company.ticker or None, company.name, now, company_id),
        )
        return company_id

    @staticmethod
    def _upsert_filing(connection: sqlite3.Connection, company_id: int, filing: Filing) -> int:
        row = connection.execute(
            "SELECT * FROM filings WHERE accession = ?", (filing.accession,)
        ).fetchone()
        values = (
            company_id,
            filing.form,
            filing.filing_date.isoformat(),
            filing.report_date.isoformat(),
            filing.primary_document,
            filing.url,
        )
        if row is None:
            cursor = connection.execute(
                "INSERT INTO filings(company_id, accession, form, filing_date, report_date, "
                "primary_document, source_url) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (company_id, filing.accession, *values[1:]),
            )
            return _last_row_id(cursor)
        existing = (
            int(row["company_id"]),
            str(row["form"]),
            str(row["filing_date"]),
            str(row["report_date"]),
            str(row["primary_document"]),
            str(row["source_url"]),
        )
        if existing != values:
            raise FilingMetadataError(
                f"Accession {filing.accession} was rediscovered with different metadata."
            )
        return int(row["id"])

    @staticmethod
    def _register_artifacts(
        connection: sqlite3.Connection,
        artifacts: tuple[InstalledArtifact, ...],
    ) -> dict[str, int]:
        ids: dict[str, int] = {}
        for artifact in artifacts:
            row = connection.execute(
                "SELECT id, relative_object_path, byte_size, media_type FROM artifacts "
                "WHERE sha256 = ?",
                (artifact.sha256,),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO artifacts(sha256, relative_object_path, byte_size, "
                    "media_type, created_at) VALUES (?, ?, ?, ?, ?)",
                    (
                        artifact.sha256,
                        artifact.relative_object_path,
                        artifact.byte_size,
                        artifact.media_type,
                        _now(),
                    ),
                )
                artifact_id = _last_row_id(cursor)
            else:
                existing = (
                    str(row["relative_object_path"]),
                    int(row["byte_size"]),
                    str(row["media_type"]),
                )
                expected = (
                    artifact.relative_object_path,
                    artifact.byte_size,
                    artifact.media_type,
                )
                if existing != expected:
                    raise SnapshotMismatchError(
                        f"Artifact metadata differs for hash {artifact.sha256}."
                    )
                artifact_id = int(row["id"])
            ids[artifact.sha256] = artifact_id
        return ids

    @staticmethod
    def _link_attempt_artifacts(
        connection: sqlite3.Connection,
        attempt_id: int,
        artifacts: tuple[InstalledArtifact, ...],
        artifact_ids: dict[str, int],
    ) -> None:
        for artifact in artifacts:
            connection.execute(
                "INSERT INTO attempt_artifacts(attempt_id, artifact_id, purpose, logical_name) "
                "VALUES (?, ?, ?, ?)",
                (
                    attempt_id,
                    artifact_ids[artifact.sha256],
                    artifact.purpose,
                    artifact.logical_name,
                ),
            )

    def _insert_snapshot_evidence(
        self,
        connection: sqlite3.Connection,
        snapshot_id: int,
        bundle: FilingEvidenceBundle,
        installed_artifacts: tuple[InstalledArtifact, ...],
        artifact_ids: dict[str, int],
    ) -> dict[str, dict[str, int]]:
        artifact_by_document = {
            artifact.source_document_key: artifact
            for artifact in installed_artifacts
            if artifact.source_document_key is not None
        }
        document_ids: dict[str, int] = {}
        for document in bundle.source_documents:
            artifact = artifact_by_document.get(document.key)
            if document.retention_kind == "retained_original" and artifact is None:
                raise ValueError(
                    f"Required source document {document.original_uri} has no artifact."
                )
            if artifact is not None and document.content_hash != artifact.sha256:
                raise SnapshotMismatchError(
                    f"Captured source hash differs for {document.original_uri}."
                )
            cursor = connection.execute(
                "INSERT INTO source_documents("
                "snapshot_id, document_key, original_uri, document_kind, retention_kind, "
                "artifact_id, content_hash, parent_uri, source_reference"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    document.key,
                    document.original_uri,
                    document.document_kind,
                    document.retention_kind,
                    artifact_ids.get(artifact.sha256) if artifact is not None else None,
                    document.content_hash,
                    document.parent_uri,
                    document.source_reference,
                ),
            )
            document_ids[document.key] = _last_row_id(cursor)

        concept_ids: dict[str, int] = {}
        for concept in bundle.concepts:
            row = connection.execute(
                "SELECT id FROM concepts WHERE namespace_uri = ? AND local_name = ?",
                (concept.namespace_uri, concept.local_name),
            ).fetchone()
            if row is None:
                cursor = connection.execute(
                    "INSERT INTO concepts(namespace_uri, local_name) VALUES (?, ?)",
                    (concept.namespace_uri, concept.local_name),
                )
                concept_id = _last_row_id(cursor)
            else:
                concept_id = int(row["id"])
            concept_ids[concept.key] = concept_id
            connection.execute(
                "INSERT INTO snapshot_concepts("
                "snapshot_id, concept_id, concept_key, display_qname, definition_status, "
                "data_type_namespace_uri, data_type_local_name, period_type, balance, "
                "is_numeric, is_abstract, source_document_id, source_locator"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    concept_id,
                    concept.key,
                    concept.display_qname,
                    concept.definition_status,
                    concept.data_type_namespace_uri,
                    concept.data_type_local_name,
                    concept.period_type,
                    concept.balance,
                    concept.is_numeric,
                    concept.is_abstract,
                    document_ids.get(concept.source_document_key or ""),
                    concept.source_locator,
                ),
            )
        for label in bundle.concept_labels:
            connection.execute(
                "INSERT INTO concept_labels("
                "snapshot_id, concept_id, role_uri, language, label_text, source_order, "
                "source_document_id, source_locator"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    concept_ids[label.concept_key],
                    label.role_uri,
                    label.language,
                    label.label_text,
                    label.source_order,
                    document_ids.get(label.source_document_key or ""),
                    label.source_locator,
                ),
            )

        context_ids: dict[str, int] = {}
        for context in bundle.contexts:
            cursor = connection.execute(
                "INSERT INTO contexts("
                "snapshot_id, context_key, source_order, source_document_id, source_locator, "
                "raw_xml, period_kind, xml_id, entity_scheme, entity_identifier, raw_start, "
                "raw_end, raw_instant, period_start_date, period_end_date, exclusive_end, "
                "segment_xml, scenario_xml, canonical_hash, legacy_report_dimensions_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    context.key,
                    context.source_order,
                    document_ids.get(context.source_document_key or ""),
                    context.source_locator,
                    context.raw_xml,
                    context.period_kind,
                    context.xml_id,
                    context.entity_scheme,
                    context.entity_identifier,
                    context.raw_start,
                    context.raw_end,
                    context.raw_instant,
                    context.period_start_date.isoformat() if context.period_start_date else None,
                    context.period_end_date.isoformat() if context.period_end_date else None,
                    context.exclusive_end,
                    context.segment_xml,
                    context.scenario_xml,
                    context.canonical_hash,
                    canonical_json(context.legacy_report_dimensions),
                ),
            )
            context_id = _last_row_id(cursor)
            context_ids[context.key] = context_id
            for dimension in context.dimensions:
                connection.execute(
                    "INSERT INTO context_dimensions("
                    "snapshot_id, context_id, source_order, axis_namespace_uri, axis_local_name, "
                    "member_kind, context_element, axis_concept_id, member_concept_id, "
                    "explicit_member_namespace_uri, explicit_member_local_name, typed_xml, "
                    "typed_text, typed_hash"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        snapshot_id,
                        context_id,
                        dimension.source_order,
                        dimension.axis_namespace_uri,
                        dimension.axis_local_name,
                        dimension.member_kind,
                        dimension.context_element,
                        concept_ids.get(dimension.axis_concept_key or ""),
                        concept_ids.get(dimension.member_concept_key or ""),
                        dimension.explicit_member_namespace_uri,
                        dimension.explicit_member_local_name,
                        dimension.typed_xml,
                        dimension.typed_text,
                        dimension.typed_hash,
                    ),
                )

        unit_ids: dict[str, int] = {}
        for unit in bundle.units:
            cursor = connection.execute(
                "INSERT INTO units("
                "snapshot_id, unit_key, source_order, source_document_id, source_locator, "
                "raw_xml, xml_id, legacy_report_unit_text, canonical_hash"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    unit.key,
                    unit.source_order,
                    document_ids.get(unit.source_document_key or ""),
                    unit.source_locator,
                    unit.raw_xml,
                    unit.xml_id,
                    unit.legacy_report_unit_text,
                    unit.canonical_hash,
                ),
            )
            unit_id = _last_row_id(cursor)
            unit_ids[unit.key] = unit_id
            for measure in unit.measures:
                connection.execute(
                    "INSERT INTO unit_measures("
                    "unit_id, side, measure_order, namespace_uri, local_name, display_qname"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        unit_id,
                        measure.side,
                        measure.measure_order,
                        measure.namespace_uri,
                        measure.local_name,
                        measure.display_qname,
                    ),
                )

        fact_ids: dict[str, int] = {}
        for observation in bundle.observations:
            cursor = connection.execute(
                "INSERT INTO facts("
                "snapshot_id, observation_key, source_order, observation_origin, fact_kind, "
                "source_document_id, source_locator, is_nil, validity_code, validity_name, "
                "concept_id, display_qname, context_id, unit_id, raw_context_ref, raw_unit_ref, "
                "xml_id, source_line, top_level_order, raw_value_text, typed_value_kind, "
                "typed_value_text, is_numeric, numeric_conversion_error, decimals, precision, "
                "language, inline_metadata_json, legacy_report_label_text"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, "
                "?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    observation.key,
                    observation.source_order,
                    observation.observation_origin,
                    observation.fact_kind,
                    document_ids.get(observation.source_document_key or ""),
                    observation.source_locator,
                    observation.is_nil,
                    observation.validity_code,
                    observation.validity_name,
                    concept_ids.get(observation.concept_key or ""),
                    observation.display_qname,
                    context_ids.get(observation.context_key or ""),
                    unit_ids.get(observation.unit_key or ""),
                    observation.raw_context_ref,
                    observation.raw_unit_ref,
                    observation.xml_id,
                    observation.source_line,
                    observation.top_level_order,
                    observation.raw_value_text,
                    observation.typed_value_kind,
                    observation.typed_value_text,
                    observation.is_numeric,
                    observation.numeric_conversion_error,
                    observation.decimals,
                    observation.precision,
                    observation.language,
                    observation.inline_metadata_json,
                    observation.legacy_report_label_text,
                ),
            )
            fact_ids[observation.key] = _last_row_id(cursor)
        for observation in bundle.observations:
            if observation.parent_fact_key is not None:
                connection.execute(
                    "UPDATE facts SET parent_fact_id = ? WHERE id = ?",
                    (fact_ids[observation.parent_fact_key], fact_ids[observation.key]),
                )

        for diagnostic in bundle.diagnostics:
            connection.execute(
                "INSERT INTO extraction_diagnostics("
                "snapshot_id, code, severity, message, source_order, fact_id, context_id, "
                "unit_id, source_document_id, raw_details_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    diagnostic.code,
                    diagnostic.severity,
                    diagnostic.message,
                    diagnostic.source_order,
                    fact_ids.get(diagnostic.fact_key or ""),
                    context_ids.get(diagnostic.context_key or ""),
                    unit_ids.get(diagnostic.unit_key or ""),
                    document_ids.get(diagnostic.source_document_key or ""),
                    diagnostic.raw_details_json,
                ),
            )

        for message in bundle.validation_messages:
            cursor = connection.execute(
                "INSERT INTO validation_messages("
                "snapshot_id, message_order, level, code, message_text, raw_record_json"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    message.message_order,
                    message.level,
                    message.code,
                    message.message_text,
                    message.raw_record_json,
                ),
            )
            message_id = _last_row_id(cursor)
            for reference in message.references:
                connection.execute(
                    "INSERT INTO validation_references("
                    "message_id, snapshot_id, reference_order, raw_reference_json, "
                    "resolution_status, fact_id, source_document_id, source_locator"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        message_id,
                        snapshot_id,
                        reference.reference_order,
                        reference.raw_reference_json,
                        reference.resolution_status,
                        fact_ids.get(reference.fact_key or ""),
                        document_ids.get(reference.source_document_key or ""),
                        reference.source_locator,
                    ),
                )

        network_ids: dict[str, int] = {}
        for network in bundle.calculation_networks:
            cursor = connection.execute(
                "INSERT INTO relationship_networks("
                "snapshot_id, arcrole_uri, extraction_status, relationship_count"
                ") VALUES (?, ?, ?, ?)",
                (
                    snapshot_id,
                    network.arcrole_uri,
                    network.extraction_status,
                    network.relationship_count,
                ),
            )
            network_ids[network.arcrole_uri] = _last_row_id(cursor)
        role_ids: dict[str, int] = {}
        for relationship in bundle.calculation_relationships:
            if relationship.role_uri not in role_ids:
                cursor = connection.execute(
                    "INSERT INTO roles(snapshot_id, role_uri, definition, source_locator) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        snapshot_id,
                        relationship.role_uri,
                        relationship.role_definition,
                        relationship.source_locator,
                    ),
                )
                role_ids[relationship.role_uri] = _last_row_id(cursor)
            connection.execute(
                "INSERT INTO calculation_arcs("
                "snapshot_id, network_id, role_id, relationship_order, parent_concept_id, "
                "child_concept_id, exact_weight_text, exact_order_text, link_namespace_uri, "
                "link_local_name, arc_namespace_uri, arc_local_name, source_document_id, "
                "source_locator, raw_arc_details_json"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    snapshot_id,
                    network_ids[relationship.arcrole_uri],
                    role_ids[relationship.role_uri],
                    relationship.relationship_order,
                    concept_ids[relationship.parent_concept_key],
                    concept_ids[relationship.child_concept_key],
                    relationship.exact_weight_text,
                    relationship.exact_order_text,
                    relationship.link_namespace_uri,
                    relationship.link_local_name,
                    relationship.arc_namespace_uri,
                    relationship.arc_local_name,
                    document_ids.get(relationship.source_document_key or ""),
                    relationship.source_locator,
                    relationship.raw_arc_details_json,
                ),
            )

        for artifact in installed_artifacts:
            connection.execute(
                "INSERT INTO snapshot_artifacts(snapshot_id, artifact_id, purpose, logical_name) "
                "VALUES (?, ?, ?, ?)",
                (
                    snapshot_id,
                    artifact_ids[artifact.sha256],
                    artifact.purpose,
                    artifact.logical_name,
                ),
            )
        return {
            "documents": document_ids,
            "concepts": concept_ids,
            "contexts": context_ids,
            "units": unit_ids,
            "facts": fact_ids,
        }

    @staticmethod
    def _existing_maps(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> dict[str, dict[str, int]]:
        return {
            "concepts": {
                str(row["concept_key"]): int(row["concept_id"])
                for row in connection.execute(
                    "SELECT concept_key, concept_id FROM snapshot_concepts WHERE snapshot_id = ?",
                    (snapshot_id,),
                )
            },
            "facts": {
                str(row["observation_key"]): int(row["id"])
                for row in connection.execute(
                    "SELECT id, observation_key FROM facts WHERE snapshot_id = ?",
                    (snapshot_id,),
                )
            },
        }

    @staticmethod
    def _insert_evaluation(
        connection: sqlite3.Connection,
        snapshot_id: int,
        evaluation: ReportEvaluation,
        maps: dict[str, dict[str, int]],
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO report_evaluations("
            "snapshot_id, report_kind, rule_version, report_date, evaluated_at"
            ") VALUES (?, ?, ?, ?, ?)",
            (
                snapshot_id,
                evaluation.report_kind,
                evaluation.rule_version,
                evaluation.report_date.isoformat(),
                _now(),
            ),
        )
        evaluation_id = _last_row_id(cursor)
        facts = maps["facts"]
        concepts = maps["concepts"]
        for status in evaluation.statuses:
            connection.execute(
                "INSERT INTO fact_report_status("
                "evaluation_id, snapshot_id, fact_id, evidence_role, selection_note, "
                "selected_fact_id) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    snapshot_id,
                    facts[status.fact_key],
                    status.evidence_role,
                    status.selection_note,
                    facts.get(status.selected_fact_key or ""),
                ),
            )
            for reason_order, (reason_code, detail) in enumerate(status.exclusion_reasons):
                connection.execute(
                    "INSERT INTO fact_exclusion_reasons("
                    "evaluation_id, snapshot_id, fact_id, reason_order, reason_code, detail"
                    ") VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        evaluation_id,
                        snapshot_id,
                        facts[status.fact_key],
                        reason_order,
                        reason_code,
                        detail,
                    ),
                )
        for issue in evaluation.reconciliation_issues:
            cursor = connection.execute(
                "INSERT INTO reconciliation_issues("
                "evaluation_id, snapshot_id, concept_id, reason_code, reason_text, issue_order"
                ") VALUES (?, ?, ?, ?, ?, ?)",
                (
                    evaluation_id,
                    snapshot_id,
                    concepts[issue.concept_key],
                    issue.reason_code,
                    issue.reason_text,
                    issue.issue_order,
                ),
            )
            issue_id = _last_row_id(cursor)
            for candidate_order, fact_key in enumerate(issue.candidate_fact_keys):
                connection.execute(
                    "INSERT INTO reconciliation_issue_facts("
                    "issue_id, snapshot_id, fact_id, candidate_order"
                    ") VALUES (?, ?, ?, ?)",
                    (issue_id, snapshot_id, facts[fact_key], candidate_order),
                )
        return evaluation_id

    @staticmethod
    def _load_source_documents(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[SourceDocumentRecord, ...]:
        return tuple(
            SourceDocumentRecord(
                key=str(row["document_key"]),
                original_uri=str(row["original_uri"]),
                document_kind=str(row["document_kind"]),
                retention_kind=str(row["retention_kind"]),  # type: ignore[arg-type]
                captured_path=(
                    str(row["relative_object_path"])
                    if row["relative_object_path"] is not None
                    else None
                ),
                content_hash=(
                    str(row["content_hash"]) if row["content_hash"] is not None else None
                ),
                byte_size=(int(row["byte_size"]) if row["byte_size"] is not None else None),
                media_type=(str(row["media_type"]) if row["media_type"] is not None else None),
                parent_uri=(str(row["parent_uri"]) if row["parent_uri"] is not None else None),
                source_reference=(
                    str(row["source_reference"]) if row["source_reference"] is not None else None
                ),
            )
            for row in connection.execute(
                "SELECT sd.*, a.relative_object_path, a.byte_size, a.media_type "
                "FROM source_documents AS sd LEFT JOIN artifacts AS a ON a.id = sd.artifact_id "
                "WHERE sd.snapshot_id = ? ORDER BY sd.id",
                (snapshot_id,),
            )
        )

    @staticmethod
    def _load_concepts(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[tuple[ConceptRecord, ...], tuple[ConceptLabelRecord, ...]]:
        document_keys = {
            int(row["id"]): str(row["document_key"])
            for row in connection.execute(
                "SELECT id, document_key FROM source_documents WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        concept_keys: dict[int, str] = {}
        concepts: list[ConceptRecord] = []
        for row in connection.execute(
            "SELECT sc.*, c.namespace_uri, c.local_name FROM snapshot_concepts AS sc "
            "JOIN concepts AS c ON c.id = sc.concept_id WHERE sc.snapshot_id = ? "
            "ORDER BY sc.concept_id",
            (snapshot_id,),
        ):
            concept_id = int(row["concept_id"])
            key = str(row["concept_key"])
            concept_keys[concept_id] = key
            concepts.append(
                ConceptRecord(
                    key=key,
                    namespace_uri=str(row["namespace_uri"]),
                    local_name=str(row["local_name"]),
                    display_qname=str(row["display_qname"]),
                    definition_status=str(row["definition_status"]),
                    data_type_namespace_uri=(
                        str(row["data_type_namespace_uri"])
                        if row["data_type_namespace_uri"] is not None
                        else None
                    ),
                    data_type_local_name=(
                        str(row["data_type_local_name"])
                        if row["data_type_local_name"] is not None
                        else None
                    ),
                    period_type=(
                        str(row["period_type"]) if row["period_type"] is not None else None
                    ),
                    balance=str(row["balance"]) if row["balance"] is not None else None,
                    is_numeric=_bool(row["is_numeric"]),
                    is_abstract=_bool(row["is_abstract"]),
                    source_document_key=document_keys.get(row["source_document_id"]),
                    source_locator=(
                        str(row["source_locator"]) if row["source_locator"] is not None else None
                    ),
                )
            )
        labels = tuple(
            ConceptLabelRecord(
                concept_key=concept_keys[int(row["concept_id"])],
                role_uri=str(row["role_uri"]),
                language=str(row["language"]),
                label_text=str(row["label_text"]),
                source_order=int(row["source_order"]),
                source_document_key=document_keys.get(row["source_document_id"]),
                source_locator=(
                    str(row["source_locator"]) if row["source_locator"] is not None else None
                ),
            )
            for row in connection.execute(
                "SELECT * FROM concept_labels WHERE snapshot_id = ? ORDER BY source_order, id",
                (snapshot_id,),
            )
        )
        return tuple(concepts), labels

    @staticmethod
    def _load_contexts(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[ContextRecord, ...]:
        document_keys = {
            int(row["id"]): str(row["document_key"])
            for row in connection.execute(
                "SELECT id, document_key FROM source_documents WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        concept_keys = {
            int(row["concept_id"]): str(row["concept_key"])
            for row in connection.execute(
                "SELECT concept_id, concept_key FROM snapshot_concepts WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        dimensions_by_context: dict[int, list[ContextDimensionRecord]] = {}
        for row in connection.execute(
            "SELECT * FROM context_dimensions WHERE snapshot_id = ? "
            "ORDER BY context_id, source_order",
            (snapshot_id,),
        ):
            dimensions_by_context.setdefault(int(row["context_id"]), []).append(
                ContextDimensionRecord(
                    source_order=int(row["source_order"]),
                    axis_namespace_uri=str(row["axis_namespace_uri"]),
                    axis_local_name=str(row["axis_local_name"]),
                    member_kind=str(row["member_kind"]),  # type: ignore[arg-type]
                    context_element=str(row["context_element"]),
                    axis_concept_key=concept_keys.get(row["axis_concept_id"]),
                    member_concept_key=concept_keys.get(row["member_concept_id"]),
                    explicit_member_namespace_uri=(
                        str(row["explicit_member_namespace_uri"])
                        if row["explicit_member_namespace_uri"] is not None
                        else None
                    ),
                    explicit_member_local_name=(
                        str(row["explicit_member_local_name"])
                        if row["explicit_member_local_name"] is not None
                        else None
                    ),
                    typed_xml=str(row["typed_xml"]) if row["typed_xml"] is not None else None,
                    typed_text=(str(row["typed_text"]) if row["typed_text"] is not None else None),
                    typed_hash=(str(row["typed_hash"]) if row["typed_hash"] is not None else None),
                )
            )
        contexts: list[ContextRecord] = []
        for row in connection.execute(
            "SELECT * FROM contexts WHERE snapshot_id = ? ORDER BY source_order", (snapshot_id,)
        ):
            contexts.append(
                ContextRecord(
                    key=str(row["context_key"]),
                    source_order=int(row["source_order"]),
                    source_document_key=document_keys.get(row["source_document_id"]),
                    source_locator=str(row["source_locator"]),
                    raw_xml=str(row["raw_xml"]),
                    period_kind=str(row["period_kind"]),  # type: ignore[arg-type]
                    xml_id=str(row["xml_id"]) if row["xml_id"] is not None else None,
                    entity_scheme=(
                        str(row["entity_scheme"]) if row["entity_scheme"] is not None else None
                    ),
                    entity_identifier=(
                        str(row["entity_identifier"])
                        if row["entity_identifier"] is not None
                        else None
                    ),
                    raw_start=(str(row["raw_start"]) if row["raw_start"] is not None else None),
                    raw_end=str(row["raw_end"]) if row["raw_end"] is not None else None,
                    raw_instant=(
                        str(row["raw_instant"]) if row["raw_instant"] is not None else None
                    ),
                    period_start_date=_optional_date(row["period_start_date"]),
                    period_end_date=_optional_date(row["period_end_date"]),
                    exclusive_end=(
                        str(row["exclusive_end"]) if row["exclusive_end"] is not None else None
                    ),
                    segment_xml=(
                        str(row["segment_xml"]) if row["segment_xml"] is not None else None
                    ),
                    scenario_xml=(
                        str(row["scenario_xml"]) if row["scenario_xml"] is not None else None
                    ),
                    canonical_hash=(
                        str(row["canonical_hash"]) if row["canonical_hash"] is not None else None
                    ),
                    legacy_report_dimensions=tuple(
                        str(item) for item in json.loads(str(row["legacy_report_dimensions_json"]))
                    ),
                    dimensions=tuple(dimensions_by_context.get(int(row["id"]), [])),
                )
            )
        return tuple(contexts)

    @staticmethod
    def _load_units(connection: sqlite3.Connection, snapshot_id: int) -> tuple[UnitRecord, ...]:
        document_keys = {
            int(row["id"]): str(row["document_key"])
            for row in connection.execute(
                "SELECT id, document_key FROM source_documents WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        measures_by_unit: dict[int, list[UnitMeasureRecord]] = {}
        for row in connection.execute(
            "SELECT um.* FROM unit_measures AS um JOIN units AS u ON u.id = um.unit_id "
            "WHERE u.snapshot_id = ? ORDER BY um.unit_id, um.side DESC, um.measure_order",
            (snapshot_id,),
        ):
            measures_by_unit.setdefault(int(row["unit_id"]), []).append(
                UnitMeasureRecord(
                    side=str(row["side"]),  # type: ignore[arg-type]
                    measure_order=int(row["measure_order"]),
                    namespace_uri=str(row["namespace_uri"]),
                    local_name=str(row["local_name"]),
                    display_qname=str(row["display_qname"]),
                )
            )
        return tuple(
            UnitRecord(
                key=str(row["unit_key"]),
                source_order=int(row["source_order"]),
                source_document_key=document_keys.get(row["source_document_id"]),
                source_locator=str(row["source_locator"]),
                raw_xml=str(row["raw_xml"]),
                xml_id=str(row["xml_id"]) if row["xml_id"] is not None else None,
                legacy_report_unit_text=(
                    str(row["legacy_report_unit_text"])
                    if row["legacy_report_unit_text"] is not None
                    else None
                ),
                canonical_hash=(
                    str(row["canonical_hash"]) if row["canonical_hash"] is not None else None
                ),
                measures=tuple(measures_by_unit.get(int(row["id"]), [])),
            )
            for row in connection.execute(
                "SELECT * FROM units WHERE snapshot_id = ? ORDER BY source_order", (snapshot_id,)
            )
        )

    @staticmethod
    def _load_observations(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[ObservationRecord, ...]:
        documents = {
            int(row["id"]): str(row["document_key"])
            for row in connection.execute(
                "SELECT id, document_key FROM source_documents WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        concepts = {
            int(row["concept_id"]): str(row["concept_key"])
            for row in connection.execute(
                "SELECT concept_id, concept_key FROM snapshot_concepts WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        contexts = {
            int(row["id"]): str(row["context_key"])
            for row in connection.execute(
                "SELECT id, context_key FROM contexts WHERE snapshot_id = ?", (snapshot_id,)
            )
        }
        units = {
            int(row["id"]): str(row["unit_key"])
            for row in connection.execute(
                "SELECT id, unit_key FROM units WHERE snapshot_id = ?", (snapshot_id,)
            )
        }
        rows = connection.execute(
            "SELECT * FROM facts WHERE snapshot_id = ? ORDER BY source_order", (snapshot_id,)
        ).fetchall()
        fact_keys = {int(row["id"]): str(row["observation_key"]) for row in rows}
        return tuple(
            ObservationRecord(
                key=str(row["observation_key"]),
                source_order=int(row["source_order"]),
                observation_origin=str(row["observation_origin"]),  # type: ignore[arg-type]
                fact_kind=str(row["fact_kind"]),  # type: ignore[arg-type]
                source_document_key=documents.get(row["source_document_id"]),
                source_locator=str(row["source_locator"]),
                is_nil=bool(row["is_nil"]),
                validity_code=(
                    int(row["validity_code"]) if row["validity_code"] is not None else None
                ),
                validity_name=str(row["validity_name"]),
                concept_key=concepts.get(row["concept_id"]),
                display_qname=(
                    str(row["display_qname"]) if row["display_qname"] is not None else None
                ),
                context_key=contexts.get(row["context_id"]),
                unit_key=units.get(row["unit_id"]),
                raw_context_ref=(
                    str(row["raw_context_ref"]) if row["raw_context_ref"] is not None else None
                ),
                raw_unit_ref=(
                    str(row["raw_unit_ref"]) if row["raw_unit_ref"] is not None else None
                ),
                parent_fact_key=fact_keys.get(row["parent_fact_id"]),
                xml_id=str(row["xml_id"]) if row["xml_id"] is not None else None,
                source_line=(int(row["source_line"]) if row["source_line"] is not None else None),
                top_level_order=(
                    int(row["top_level_order"]) if row["top_level_order"] is not None else None
                ),
                raw_value_text=(
                    str(row["raw_value_text"]) if row["raw_value_text"] is not None else None
                ),
                typed_value_kind=(
                    str(row["typed_value_kind"]) if row["typed_value_kind"] is not None else None
                ),
                typed_value_text=(
                    str(row["typed_value_text"]) if row["typed_value_text"] is not None else None
                ),
                is_numeric=_bool(row["is_numeric"]),
                numeric_conversion_error=(
                    str(row["numeric_conversion_error"])
                    if row["numeric_conversion_error"] is not None
                    else None
                ),
                decimals=(str(row["decimals"]) if row["decimals"] is not None else None),
                precision=(str(row["precision"]) if row["precision"] is not None else None),
                language=(str(row["language"]) if row["language"] is not None else None),
                inline_metadata_json=(
                    str(row["inline_metadata_json"])
                    if row["inline_metadata_json"] is not None
                    else None
                ),
                legacy_report_label_text=(
                    str(row["legacy_report_label_text"])
                    if row["legacy_report_label_text"] is not None
                    else None
                ),
            )
            for row in rows
        )

    @staticmethod
    def _load_diagnostics(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[ExtractionDiagnosticRecord, ...]:
        maps = EvidenceStore._key_maps(connection, snapshot_id)
        return tuple(
            ExtractionDiagnosticRecord(
                code=str(row["code"]),
                severity=str(row["severity"]),
                message=str(row["message"]),
                source_order=int(row["source_order"]),
                fact_key=maps["facts"].get(row["fact_id"]),
                context_key=maps["contexts"].get(row["context_id"]),
                unit_key=maps["units"].get(row["unit_id"]),
                source_document_key=maps["documents"].get(row["source_document_id"]),
                raw_details_json=(
                    str(row["raw_details_json"]) if row["raw_details_json"] is not None else None
                ),
            )
            for row in connection.execute(
                "SELECT * FROM extraction_diagnostics WHERE snapshot_id = ? ORDER BY source_order",
                (snapshot_id,),
            )
        )

    @staticmethod
    def _load_validation(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[ValidationRecord, ...]:
        maps = EvidenceStore._key_maps(connection, snapshot_id)
        references: dict[int, list[ValidationReferenceRecord]] = {}
        for row in connection.execute(
            "SELECT * FROM validation_references WHERE snapshot_id = ? "
            "ORDER BY message_id, reference_order",
            (snapshot_id,),
        ):
            references.setdefault(int(row["message_id"]), []).append(
                ValidationReferenceRecord(
                    reference_order=int(row["reference_order"]),
                    raw_reference_json=str(row["raw_reference_json"]),
                    resolution_status=str(row["resolution_status"]),  # type: ignore[arg-type]
                    fact_key=maps["facts"].get(row["fact_id"]),
                    source_document_key=maps["documents"].get(row["source_document_id"]),
                    source_locator=(
                        str(row["source_locator"]) if row["source_locator"] is not None else None
                    ),
                )
            )
        return tuple(
            ValidationRecord(
                message_order=int(row["message_order"]),
                level=str(row["level"]),
                code=str(row["code"]),
                message_text=str(row["message_text"]),
                raw_record_json=str(row["raw_record_json"]),
                references=tuple(references.get(int(row["id"]), [])),
            )
            for row in connection.execute(
                "SELECT * FROM validation_messages WHERE snapshot_id = ? ORDER BY message_order",
                (snapshot_id,),
            )
        )

    @staticmethod
    def _load_calculations(
        connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[tuple[CalculationNetworkRecord, ...], tuple[CalculationRelationshipRecord, ...]]:
        networks = tuple(
            CalculationNetworkRecord(
                arcrole_uri=str(row["arcrole_uri"]),
                extraction_status=str(row["extraction_status"]),  # type: ignore[arg-type]
                relationship_count=int(row["relationship_count"]),
            )
            for row in connection.execute(
                "SELECT * FROM relationship_networks WHERE snapshot_id = ? ORDER BY arcrole_uri",
                (snapshot_id,),
            )
        )
        concept_keys = {
            int(row["concept_id"]): str(row["concept_key"])
            for row in connection.execute(
                "SELECT concept_id, concept_key FROM snapshot_concepts WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        documents = {
            int(row["id"]): str(row["document_key"])
            for row in connection.execute(
                "SELECT id, document_key FROM source_documents WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        relationships = tuple(
            CalculationRelationshipRecord(
                arcrole_uri=str(row["arcrole_uri"]),
                role_uri=str(row["role_uri"]),
                relationship_order=int(row["relationship_order"]),
                parent_concept_key=concept_keys[int(row["parent_concept_id"])],
                child_concept_key=concept_keys[int(row["child_concept_id"])],
                exact_weight_text=str(row["exact_weight_text"]),
                exact_order_text=(
                    str(row["exact_order_text"]) if row["exact_order_text"] is not None else None
                ),
                role_definition=(str(row["definition"]) if row["definition"] is not None else None),
                link_namespace_uri=(
                    str(row["link_namespace_uri"])
                    if row["link_namespace_uri"] is not None
                    else None
                ),
                link_local_name=(
                    str(row["link_local_name"]) if row["link_local_name"] is not None else None
                ),
                arc_namespace_uri=(
                    str(row["arc_namespace_uri"]) if row["arc_namespace_uri"] is not None else None
                ),
                arc_local_name=(
                    str(row["arc_local_name"]) if row["arc_local_name"] is not None else None
                ),
                source_document_key=documents.get(row["source_document_id"]),
                source_locator=(
                    str(row["source_locator"]) if row["source_locator"] is not None else None
                ),
                raw_arc_details_json=(
                    str(row["raw_arc_details_json"])
                    if row["raw_arc_details_json"] is not None
                    else None
                ),
            )
            for row in connection.execute(
                "SELECT ca.*, rn.arcrole_uri, r.role_uri, r.definition "
                "FROM calculation_arcs AS ca "
                "JOIN relationship_networks AS rn ON rn.id = ca.network_id "
                "JOIN roles AS r ON r.id = ca.role_id "
                "WHERE ca.snapshot_id = ? ORDER BY rn.arcrole_uri, r.role_uri, "
                "ca.relationship_order",
                (snapshot_id,),
            )
        )
        return networks, relationships

    def _load_raw_log(self, connection: sqlite3.Connection, snapshot_id: int) -> str:
        row = connection.execute(
            "SELECT a.relative_object_path, a.sha256, a.byte_size "
            "FROM snapshot_artifacts AS sa JOIN artifacts AS a ON a.id = sa.artifact_id "
            "WHERE sa.snapshot_id = ? AND sa.purpose = 'arelle-log' "
            "ORDER BY sa.logical_name LIMIT 1",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return ""
        path = self.artifacts.resolve(
            str(row["relative_object_path"]), str(row["sha256"]), int(row["byte_size"])
        )
        return path.read_text(encoding="utf-8")

    @staticmethod
    def _key_maps(connection: sqlite3.Connection, snapshot_id: int) -> dict[str, dict[int, str]]:
        return {
            "documents": {
                int(row["id"]): str(row["document_key"])
                for row in connection.execute(
                    "SELECT id, document_key FROM source_documents WHERE snapshot_id = ?",
                    (snapshot_id,),
                )
            },
            "contexts": {
                int(row["id"]): str(row["context_key"])
                for row in connection.execute(
                    "SELECT id, context_key FROM contexts WHERE snapshot_id = ?", (snapshot_id,)
                )
            },
            "units": {
                int(row["id"]): str(row["unit_key"])
                for row in connection.execute(
                    "SELECT id, unit_key FROM units WHERE snapshot_id = ?", (snapshot_id,)
                )
            },
            "facts": {
                int(row["id"]): str(row["observation_key"])
                for row in connection.execute(
                    "SELECT id, observation_key FROM facts WHERE snapshot_id = ?", (snapshot_id,)
                )
            },
        }

    @staticmethod
    def _load_evaluation(connection: sqlite3.Connection, evaluation_id: int) -> ReportEvaluation:
        evaluation = connection.execute(
            "SELECT * FROM report_evaluations WHERE id = ?", (evaluation_id,)
        ).fetchone()
        if evaluation is None:
            raise KeyError(f"Unknown report evaluation {evaluation_id}.")
        snapshot_id = int(evaluation["snapshot_id"])
        fact_keys = {
            int(row["id"]): str(row["observation_key"])
            for row in connection.execute(
                "SELECT id, observation_key FROM facts WHERE snapshot_id = ?", (snapshot_id,)
            )
        }
        concept_keys = {
            int(row["concept_id"]): str(row["concept_key"])
            for row in connection.execute(
                "SELECT concept_id, concept_key FROM snapshot_concepts WHERE snapshot_id = ?",
                (snapshot_id,),
            )
        }
        reasons: dict[int, list[tuple[str, str]]] = {}
        for row in connection.execute(
            "SELECT * FROM fact_exclusion_reasons WHERE evaluation_id = ? "
            "ORDER BY fact_id, reason_order",
            (evaluation_id,),
        ):
            reasons.setdefault(int(row["fact_id"]), []).append(
                (str(row["reason_code"]), str(row["detail"]))
            )
        statuses = tuple(
            FactReportStatus(
                fact_key=fact_keys[int(row["fact_id"])],
                evidence_role=str(row["evidence_role"]),  # type: ignore[arg-type]
                exclusion_reasons=tuple(reasons.get(int(row["fact_id"]), [])),
                selection_note=(
                    str(row["selection_note"]) if row["selection_note"] is not None else None
                ),
                selected_fact_key=fact_keys.get(row["selected_fact_id"]),
            )
            for row in connection.execute(
                "SELECT frs.* FROM fact_report_status AS frs JOIN facts AS f ON f.id = frs.fact_id "
                "WHERE frs.evaluation_id = ? ORDER BY f.source_order",
                (evaluation_id,),
            )
        )
        issues: list[ReconciliationIssueRecord] = []
        for row in connection.execute(
            "SELECT * FROM reconciliation_issues WHERE evaluation_id = ? ORDER BY issue_order",
            (evaluation_id,),
        ):
            issue_id = int(row["id"])
            candidates = tuple(
                fact_keys[int(candidate["fact_id"])]
                for candidate in connection.execute(
                    "SELECT fact_id FROM reconciliation_issue_facts WHERE issue_id = ? "
                    "ORDER BY candidate_order",
                    (issue_id,),
                )
            )
            issues.append(
                ReconciliationIssueRecord(
                    concept_key=concept_keys[int(row["concept_id"])],
                    reason_code=str(row["reason_code"]),
                    reason_text=str(row["reason_text"]),
                    issue_order=int(row["issue_order"]),
                    candidate_fact_keys=candidates,
                )
            )
        return ReportEvaluation(
            report_kind=str(evaluation["report_kind"]),  # type: ignore[arg-type]
            rule_version=str(evaluation["rule_version"]),
            report_date=date.fromisoformat(str(evaluation["report_date"])),
            statuses=statuses,
            reconciliation_issues=tuple(issues),
        )

    def _verify_snapshot_artifacts(
        self, connection: sqlite3.Connection, snapshot_id: int
    ) -> tuple[dict[str, object], ...]:
        results: list[dict[str, object]] = []
        for row in connection.execute(
            "SELECT a.id, a.sha256, a.relative_object_path, a.byte_size, "
            "sa.purpose, sa.logical_name FROM snapshot_artifacts AS sa "
            "JOIN artifacts AS a ON a.id = sa.artifact_id WHERE sa.snapshot_id = ? "
            "ORDER BY sa.purpose, sa.logical_name",
            (snapshot_id,),
        ):
            path = self.artifacts.resolve(
                str(row["relative_object_path"]),
                str(row["sha256"]),
                int(row["byte_size"]),
            )
            result = dict(row)
            result["verified_path"] = str(path)
            results.append(result)
        return tuple(results)


def _split_concept_key(value: str) -> tuple[str, str]:
    if value.startswith("{") and "}" in value:
        namespace, local_name = value[1:].split("}", maxsplit=1)
        return namespace, local_name
    namespace, separator, local_name = value.rpartition(":")
    if separator:
        return namespace, local_name
    return "", value


def _last_row_id(cursor: sqlite3.Cursor) -> int:
    if cursor.lastrowid is None:
        raise RuntimeError("SQLite insert did not return a row ID.")
    return cursor.lastrowid
