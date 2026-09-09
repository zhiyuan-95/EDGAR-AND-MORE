from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Literal, Protocol

from sec_inline_financials.evidence_classification import classify_report
from sec_inline_financials.evidence_models import (
    ExtractionProfile,
    FilingEvidenceBundle,
    FilingOutcome,
    InstalledArtifact,
    RunOutcome,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.service import SecGateway
from sec_inline_financials.storage.evidence_store import EvidenceStore
from sec_inline_financials.storage.fingerprints import (
    canonical_json,
    extraction_profile_hash,
    sha256_text,
    source_manifest_json,
)


class EvidenceProcessor(Protocol):
    def extraction_profile(self) -> ExtractionProfile: ...

    def extract_evidence(
        self, company: Company, filing: Filing, capture_area: Path
    ) -> FilingEvidenceBundle: ...


class EvidenceIngestionService:
    """Orchestrate explicit evidence ingestion without invoking report rendering."""

    def __init__(
        self,
        *,
        sec_client: SecGateway,
        processor: EvidenceProcessor,
        store: EvidenceStore,
    ) -> None:
        self._sec_client = sec_client
        self._processor = processor
        self._store = store

    def ingest_filing(self, company: Company, filing: Filing) -> FilingOutcome:
        run_id = self._store.create_processing_run(
            company,
            purpose="explicit-filing-ingest",
            requested_window={"accessions": [filing.accession]},
        )
        outcome = self._ingest_with_run(run_id, company, filing)
        self._store.finish_processing_run(run_id)
        return outcome

    def ingest_company_window(
        self, ticker: str, annual_count: int = 5, quarterly_count: int = 12
    ) -> RunOutcome:
        company = self._sec_client.resolve_company(ticker)
        annual = self._sec_client.discover_annual_inline_filings(company, count=annual_count)
        quarterly = self._sec_client.discover_quarterly_inline_filings(
            company, count=quarterly_count
        )
        run_id = self._store.create_processing_run(
            company,
            purpose="explicit-company-window-ingest",
            requested_window={"annual_count": annual_count, "quarterly_count": quarterly_count},
        )
        filings = {filing.accession: filing for filing in (*annual, *quarterly)}
        outcomes = tuple(
            self._ingest_with_run(run_id, company, filing)
            for filing in sorted(
                filings.values(),
                key=lambda item: (item.report_date, item.form, item.accession),
                reverse=True,
            )
        )
        status = self._store.finish_processing_run(run_id)
        return RunOutcome(run_id=run_id, status=status, filings=outcomes)

    def _ingest_with_run(self, run_id: int, company: Company, filing: Filing) -> FilingOutcome:
        attempt_id = self._store.begin_filing_attempt(run_id, company, filing)
        attempt_identity = str(attempt_id)
        capture_area: Path | None = None
        bundle: FilingEvidenceBundle | None = None
        try:
            profile = self._processor.extraction_profile()
            profile_hash = sha256_text(canonical_json(profile))
            reusable = self._store.find_reusable_snapshot(filing.accession, profile_hash)
            report_kind: Literal["annual", "quarterly"] = (
                "quarterly" if filing.form == "10-Q" else "annual"
            )
            if reusable is not None:
                stored_evaluation = self._store.get_report_evaluation(
                    reusable.snapshot_id, report_kind, "report-v1"
                )
                if stored_evaluation is None:
                    bundle = self._store.load_snapshot(reusable.snapshot_id)
                    self._store.save_report_evaluation(
                        reusable.snapshot_id, classify_report(bundle, report_kind)
                    )
                self._store.mark_attempt_reused(attempt_id, reusable.snapshot_id)
                return FilingOutcome(
                    accession=filing.accession,
                    status="reused",
                    snapshot_id=reusable.snapshot_id,
                )

            capture_area = self._store.artifacts.stage_directory(attempt_identity)
            bundle = self._processor.extract_evidence(company, filing, capture_area)
            if extraction_profile_hash(bundle) != profile_hash:
                raise ValueError("Processor extraction profile changed during one filing attempt.")
            evaluation = classify_report(bundle, report_kind)
            installed = self._install_bundle_artifacts(bundle, attempt_identity=attempt_identity)
            result = self._store.save_snapshot(bundle, evaluation, attempt_id, installed)
            self._store.artifacts.cleanup_staging(attempt_identity)
            return FilingOutcome(
                accession=filing.accession,
                status=result.disposition,
                snapshot_id=result.snapshot_id,
            )
        except Exception as exc:
            if bundle is not None:
                recovered = self._store.find_committed_attempt(attempt_id, bundle)
                if recovered is not None:
                    with suppress(Exception):
                        self._store.artifacts.cleanup_staging(attempt_identity)
                    return FilingOutcome(
                        accession=filing.accession,
                        status=recovered.disposition,
                        snapshot_id=recovered.snapshot_id,
                    )
            if capture_area is not None:
                raw_log_path = capture_area / "raw-arelle-log.json"
                if raw_log_path.is_file():
                    with suppress(Exception):
                        failed_log = self._store.artifacts.install(
                            raw_log_path,
                            purpose="failed-arelle-log",
                            logical_name="arelle-log.json",
                            media_type="application/json",
                        )
                        self._store.register_attempt_artifacts(attempt_id, (failed_log,))
            self._store.mark_attempt_failed(attempt_id, exc)
            return FilingOutcome(
                accession=filing.accession,
                status="failed",
                error=f"{type(exc).__name__}: {exc}",
            )

    def _install_bundle_artifacts(
        self, bundle: FilingEvidenceBundle, *, attempt_identity: str
    ) -> tuple[InstalledArtifact, ...]:
        installed: list[InstalledArtifact] = []
        for index, document in enumerate(bundle.source_documents):
            if document.captured_path is None:
                continue
            installed.append(
                self._store.artifacts.install(
                    Path(document.captured_path),
                    purpose="source-document",
                    logical_name=f"source-{index:04d}",
                    media_type=document.media_type or "application/octet-stream",
                    source_document_key=document.key,
                )
            )
        log_path = self._store.artifacts.stage_bytes(
            attempt_identity, "arelle-log.json", bundle.raw_log_json.encode("utf-8")
        )
        installed.append(
            self._store.artifacts.install(
                log_path,
                purpose="arelle-log",
                logical_name="arelle-log.json",
                media_type="application/json",
            )
        )
        manifest_path = self._store.artifacts.stage_bytes(
            attempt_identity,
            "source-manifest.json",
            source_manifest_json(bundle).encode("utf-8"),
        )
        installed.append(
            self._store.artifacts.install(
                manifest_path,
                purpose="source-manifest",
                logical_name="source-manifest.json",
                media_type="application/json",
            )
        )
        return tuple(installed)
