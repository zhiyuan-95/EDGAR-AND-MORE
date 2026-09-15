from __future__ import annotations

import os
from pathlib import Path

from arelle.api.Session import Session
from arelle.RuntimeOptions import RuntimeOptions

from sec_inline_financials.errors import ProcessingError
from sec_inline_financials.evidence_extraction import (
    build_extraction_profile,
    detach_filing_evidence,
)
from sec_inline_financials.evidence_models import ExtractionProfile, FilingEvidenceBundle
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.sec_transform_plugin import ensure_sec_transform_plugin


class ArelleProcessor:
    """Load one Inline XBRL filing and detach its complete evidence from Arelle."""

    def __init__(self, *, user_agent: str, cache_directory: Path | None = None) -> None:
        self._user_agent = user_agent
        self._cache_directory = cache_directory

    def extract_evidence(
        self, company: Company, filing: Filing, capture_area: Path
    ) -> FilingEvidenceBundle:
        """Capture and detach complete filing evidence while Arelle is still open."""
        plugin_cache = (
            self._cache_directory.parent
            if self._cache_directory is not None
            else capture_area.parent
        )
        sec_transform_plugin = ensure_sec_transform_plugin(plugin_cache)
        attempt_cache = capture_area / "arelle-cache"
        options = RuntimeOptions(
            entrypointFile=filing.url,
            internetConnectivity="online",
            internetTimeout=60,
            httpUserAgent=self._user_agent,
            cacheDirectory=str(attempt_cache),
            keepOpen=True,
            validate=True,
            calcs="c10d",
            validateDuplicateFacts="all",
            plugins=str(sec_transform_plugin),
            logFile="logToBuffer",
            logLevel="WARNING",
            logTextMaxLength=10_000,
            disablePersistentConfig=True,
        )
        with Session() as session:
            succeeded = session.run(options)
            models = session.get_models()
            if not succeeded or not models:
                raise ProcessingError(f"Arelle could not load {filing.accession} ({filing.url}).")
            raw_log_json = session.get_logs("json")
            capture_area.mkdir(parents=True, exist_ok=True)
            raw_log_path = capture_area / "raw-arelle-log.json"
            with raw_log_path.open("xb") as stream:
                stream.write(raw_log_json.encode("utf-8"))
                stream.flush()
                os.fsync(stream.fileno())
            return detach_filing_evidence(
                models[0],
                company=company,
                filing=filing,
                raw_log_json=raw_log_json,
                capture_area=capture_area / "captured-sources",
                extraction_profile=build_extraction_profile(sec_transform_plugin),
            )

    def extraction_profile(self) -> ExtractionProfile:
        plugin_cache = (
            self._cache_directory.parent
            if self._cache_directory is not None
            else Path.cwd() / ".cache"
        )
        return build_extraction_profile(ensure_sec_transform_plugin(plugin_cache))
