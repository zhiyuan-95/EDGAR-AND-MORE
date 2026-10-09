from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from sec_inline_financials.evidence_models import (
    CoverageManifest,
    ExtractionProfile,
    FilingEvidenceBundle,
)
from sec_inline_financials.models import Company, Filing
from sec_inline_financials.storage.fingerprints import payload_hash, payload_json


@pytest.mark.parametrize("serialization_version", ("evidence-v1", "evidence-v2"))
def test_legacy_payload_hashes_ignore_new_filing_provenance_fields(
    serialization_version: str,
) -> None:
    original = _bundle(serialization_version)
    changed = replace(
        original,
        filing=replace(
            original.filing,
            registrant_cik="0000000002",
            archive_owner_cik="0000000003",
        ),
    )

    assert payload_hash(changed) == payload_hash(original)
    assert "registrant_cik" not in payload_json(original)
    assert "archive_owner_cik" not in payload_json(original)


@pytest.mark.parametrize("field_name", ("registrant_cik", "archive_owner_cik"))
def test_v3_payload_hash_changes_with_filing_provenance(field_name: str) -> None:
    original = _bundle("evidence-v3")
    changed = replace(
        original,
        filing=replace(original.filing, **{field_name: "0000000002"}),
    )

    assert payload_hash(changed) != payload_hash(original)
    assert '"version":"evidence-payload-v3"' in payload_json(original)


def _bundle(serialization_version: str) -> FilingEvidenceBundle:
    company = Company(ticker="TEST", cik="0000000001", name="Test Company")
    filing = Filing(
        accession="0000000001-26-000001",
        filing_date=date(2026, 2, 1),
        report_date=date(2025, 12, 31),
        form="10-K",
        primary_document="test.htm",
        url="https://www.sec.gov/Archives/test.htm",
        registrant_cik=company.cik,
        archive_owner_cik=company.cik,
    )
    return FilingEvidenceBundle(
        company=company,
        filing=filing,
        captured_company_name=company.name,
        captured_company_ticker=company.ticker,
        fiscal_year=2025,
        fiscal_period="FY",
        fiscal_year_source="test",
        fiscal_period_source="test",
        source_documents=(),
        concepts=(),
        concept_labels=(),
        contexts=(),
        units=(),
        observations=(),
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
            serialization_version=serialization_version,
        ),
        coverage_manifest=CoverageManifest(
            recognized_fact_count=0,
            unresolved_observation_count=0,
            numeric_count=0,
            nonnumeric_count=0,
            nil_count=0,
            invalid_count=0,
            context_count=0,
            unit_count=0,
            validation_message_count=0,
            calculation_relationship_count=0,
            source_document_count=0,
        ),
        raw_log_json="{}",
    )
