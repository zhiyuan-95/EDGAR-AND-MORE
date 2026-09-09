CREATE TABLE schema_migrations (
    version INTEGER PRIMARY KEY CHECK (version > 0),
    filename TEXT NOT NULL UNIQUE,
    checksum TEXT NOT NULL,
    applied_at TEXT NOT NULL
);

CREATE TABLE companies (
    id INTEGER PRIMARY KEY,
    cik TEXT NOT NULL UNIQUE CHECK (length(cik) = 10),
    ticker TEXT,
    current_name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE filings (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    accession TEXT NOT NULL UNIQUE,
    form TEXT NOT NULL,
    filing_date TEXT NOT NULL,
    report_date TEXT NOT NULL,
    primary_document TEXT NOT NULL,
    source_url TEXT NOT NULL
);

CREATE TABLE processing_runs (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    purpose TEXT NOT NULL,
    requested_window_json TEXT NOT NULL,
    owner_pid INTEGER NOT NULL CHECK (owner_pid > 0),
    owner_process_start_identity TEXT NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    error_summary TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('running', 'succeeded', 'partial', 'failed', 'interrupted')
    )
);

CREATE TABLE processing_run_filings (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES processing_runs(id),
    filing_id INTEGER NOT NULL REFERENCES filings(id),
    status TEXT NOT NULL CHECK (
        status IN ('pending', 'processing', 'stored', 'reused', 'failed', 'interrupted')
    ),
    started_at TEXT NOT NULL,
    completed_at TEXT,
    snapshot_id INTEGER,
    error_code TEXT,
    error_text TEXT,
    UNIQUE (run_id, filing_id),
    UNIQUE (id, filing_id),
    FOREIGN KEY (snapshot_id) REFERENCES evidence_snapshots(id) DEFERRABLE INITIALLY DEFERRED
);

CREATE TABLE evidence_snapshots (
    id INTEGER PRIMARY KEY,
    filing_id INTEGER NOT NULL REFERENCES filings(id),
    completed_by_attempt_id INTEGER NOT NULL,
    captured_company_name TEXT NOT NULL,
    captured_company_ticker TEXT,
    captured_filing_metadata_json TEXT NOT NULL,
    fiscal_year INTEGER,
    fiscal_period TEXT,
    fiscal_year_source TEXT,
    fiscal_period_source TEXT,
    source_manifest_hash TEXT NOT NULL CHECK (length(source_manifest_hash) = 64),
    extraction_profile_hash TEXT NOT NULL CHECK (length(extraction_profile_hash) = 64),
    extraction_profile_json TEXT NOT NULL,
    payload_hash TEXT NOT NULL CHECK (length(payload_hash) = 64),
    coverage_manifest_json TEXT NOT NULL,
    captured_at TEXT NOT NULL,
    UNIQUE (filing_id, source_manifest_hash, extraction_profile_hash),
    UNIQUE (id, filing_id),
    FOREIGN KEY (completed_by_attempt_id, filing_id)
        REFERENCES processing_run_filings(id, filing_id)
);

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    sha256 TEXT NOT NULL UNIQUE CHECK (length(sha256) = 64),
    relative_object_path TEXT NOT NULL UNIQUE,
    byte_size INTEGER NOT NULL CHECK (byte_size >= 0),
    media_type TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE snapshot_artifacts (
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    purpose TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    PRIMARY KEY (snapshot_id, purpose, logical_name)
);

CREATE TABLE attempt_artifacts (
    attempt_id INTEGER NOT NULL REFERENCES processing_run_filings(id),
    artifact_id INTEGER NOT NULL REFERENCES artifacts(id),
    purpose TEXT NOT NULL,
    logical_name TEXT NOT NULL,
    PRIMARY KEY (attempt_id, purpose, logical_name)
);

CREATE TABLE source_documents (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    document_key TEXT NOT NULL,
    original_uri TEXT NOT NULL,
    document_kind TEXT NOT NULL,
    retention_kind TEXT NOT NULL CHECK (
        retention_kind IN (
            'retained_original', 'external_dependency_reference', 'generated_document'
        )
    ),
    artifact_id INTEGER REFERENCES artifacts(id),
    content_hash TEXT,
    parent_uri TEXT,
    source_reference TEXT,
    UNIQUE (snapshot_id, document_key),
    UNIQUE (snapshot_id, original_uri),
    UNIQUE (snapshot_id, id)
);

CREATE TABLE concepts (
    id INTEGER PRIMARY KEY,
    namespace_uri TEXT NOT NULL,
    local_name TEXT NOT NULL,
    UNIQUE (namespace_uri, local_name)
);

CREATE TABLE snapshot_concepts (
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    concept_id INTEGER NOT NULL REFERENCES concepts(id),
    concept_key TEXT NOT NULL,
    display_qname TEXT NOT NULL,
    definition_status TEXT NOT NULL,
    data_type_namespace_uri TEXT,
    data_type_local_name TEXT,
    period_type TEXT,
    balance TEXT,
    is_numeric INTEGER CHECK (is_numeric IN (0, 1) OR is_numeric IS NULL),
    is_abstract INTEGER CHECK (is_abstract IN (0, 1) OR is_abstract IS NULL),
    source_document_id INTEGER,
    source_locator TEXT,
    PRIMARY KEY (snapshot_id, concept_id),
    UNIQUE (snapshot_id, concept_key),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id)
);

CREATE TABLE concept_labels (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL,
    concept_id INTEGER NOT NULL,
    role_uri TEXT NOT NULL,
    language TEXT NOT NULL,
    label_text TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    source_document_id INTEGER,
    source_locator TEXT,
    UNIQUE (snapshot_id, concept_id, role_uri, language, source_order),
    FOREIGN KEY (snapshot_id, concept_id)
        REFERENCES snapshot_concepts(snapshot_id, concept_id),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id)
);

CREATE TABLE contexts (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    context_key TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    source_document_id INTEGER,
    source_locator TEXT NOT NULL,
    raw_xml TEXT NOT NULL,
    period_kind TEXT NOT NULL CHECK (period_kind IN ('instant', 'duration', 'forever', 'unknown')),
    xml_id TEXT,
    entity_scheme TEXT,
    entity_identifier TEXT,
    raw_start TEXT,
    raw_end TEXT,
    raw_instant TEXT,
    period_start_date TEXT,
    period_end_date TEXT,
    exclusive_end TEXT,
    segment_xml TEXT,
    scenario_xml TEXT,
    canonical_hash TEXT,
    legacy_report_dimensions_json TEXT NOT NULL,
    UNIQUE (snapshot_id, source_order),
    UNIQUE (snapshot_id, context_key),
    UNIQUE (snapshot_id, id),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id)
);

CREATE TABLE context_dimensions (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL,
    context_id INTEGER NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    axis_namespace_uri TEXT NOT NULL,
    axis_local_name TEXT NOT NULL,
    member_kind TEXT NOT NULL CHECK (member_kind IN ('explicit', 'typed', 'unresolved')),
    context_element TEXT NOT NULL,
    axis_concept_id INTEGER,
    member_concept_id INTEGER,
    explicit_member_namespace_uri TEXT,
    explicit_member_local_name TEXT,
    typed_xml TEXT,
    typed_text TEXT,
    typed_hash TEXT,
    UNIQUE (context_id, source_order),
    CHECK (
        (member_kind = 'explicit' AND explicit_member_local_name IS NOT NULL
            AND typed_xml IS NULL AND typed_text IS NULL)
        OR (member_kind = 'typed' AND explicit_member_namespace_uri IS NULL
            AND explicit_member_local_name IS NULL AND typed_xml IS NOT NULL)
        OR (member_kind = 'unresolved' AND explicit_member_namespace_uri IS NULL
            AND explicit_member_local_name IS NULL)
    ),
    FOREIGN KEY (snapshot_id, context_id) REFERENCES contexts(snapshot_id, id),
    FOREIGN KEY (snapshot_id, axis_concept_id)
        REFERENCES snapshot_concepts(snapshot_id, concept_id),
    FOREIGN KEY (snapshot_id, member_concept_id)
        REFERENCES snapshot_concepts(snapshot_id, concept_id)
);

CREATE TABLE units (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    unit_key TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    source_document_id INTEGER,
    source_locator TEXT NOT NULL,
    raw_xml TEXT NOT NULL,
    xml_id TEXT,
    legacy_report_unit_text TEXT,
    canonical_hash TEXT,
    UNIQUE (snapshot_id, source_order),
    UNIQUE (snapshot_id, unit_key),
    UNIQUE (snapshot_id, id),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id)
);

CREATE TABLE unit_measures (
    unit_id INTEGER NOT NULL REFERENCES units(id),
    side TEXT NOT NULL CHECK (side IN ('numerator', 'denominator')),
    measure_order INTEGER NOT NULL CHECK (measure_order >= 0),
    namespace_uri TEXT NOT NULL,
    local_name TEXT NOT NULL,
    display_qname TEXT NOT NULL,
    PRIMARY KEY (unit_id, side, measure_order)
);

CREATE TABLE facts (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    observation_key TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    observation_origin TEXT NOT NULL CHECK (observation_origin IN ('recognized', 'undefined')),
    fact_kind TEXT NOT NULL CHECK (fact_kind IN ('item', 'tuple', 'unresolved')),
    source_document_id INTEGER,
    source_locator TEXT NOT NULL,
    is_nil INTEGER NOT NULL CHECK (is_nil IN (0, 1)),
    validity_code INTEGER,
    validity_name TEXT NOT NULL,
    concept_id INTEGER,
    display_qname TEXT,
    context_id INTEGER,
    unit_id INTEGER,
    raw_context_ref TEXT,
    raw_unit_ref TEXT,
    parent_fact_id INTEGER,
    xml_id TEXT,
    source_line INTEGER,
    top_level_order INTEGER,
    raw_value_text TEXT,
    typed_value_kind TEXT,
    typed_value_text TEXT,
    is_numeric INTEGER CHECK (is_numeric IN (0, 1) OR is_numeric IS NULL),
    numeric_conversion_error TEXT,
    decimals TEXT,
    precision TEXT,
    language TEXT,
    inline_metadata_json TEXT,
    legacy_report_label_text TEXT,
    UNIQUE (snapshot_id, source_order),
    UNIQUE (snapshot_id, observation_key),
    UNIQUE (snapshot_id, id),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id),
    FOREIGN KEY (snapshot_id, concept_id)
        REFERENCES snapshot_concepts(snapshot_id, concept_id),
    FOREIGN KEY (snapshot_id, context_id) REFERENCES contexts(snapshot_id, id),
    FOREIGN KEY (snapshot_id, unit_id) REFERENCES units(snapshot_id, id),
    FOREIGN KEY (snapshot_id, parent_fact_id) REFERENCES facts(snapshot_id, id)
);

CREATE TABLE extraction_diagnostics (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    code TEXT NOT NULL,
    severity TEXT NOT NULL,
    message TEXT NOT NULL,
    source_order INTEGER NOT NULL CHECK (source_order >= 0),
    fact_id INTEGER,
    context_id INTEGER,
    unit_id INTEGER,
    source_document_id INTEGER,
    raw_details_json TEXT,
    UNIQUE (snapshot_id, source_order),
    FOREIGN KEY (snapshot_id, fact_id) REFERENCES facts(snapshot_id, id),
    FOREIGN KEY (snapshot_id, context_id) REFERENCES contexts(snapshot_id, id),
    FOREIGN KEY (snapshot_id, unit_id) REFERENCES units(snapshot_id, id),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id)
);

CREATE TABLE report_evaluations (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    report_kind TEXT NOT NULL CHECK (report_kind IN ('annual', 'quarterly')),
    rule_version TEXT NOT NULL,
    report_date TEXT NOT NULL,
    evaluated_at TEXT NOT NULL,
    UNIQUE (snapshot_id, report_kind, rule_version),
    UNIQUE (snapshot_id, id)
);

CREATE TABLE fact_report_status (
    evaluation_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    evidence_role TEXT NOT NULL CHECK (
        evidence_role IN (
            'selected_primary', 'dimensional', 'duplicate_not_selected',
            'conflict_candidate', 'excluded'
        )
    ),
    selection_note TEXT,
    selected_fact_id INTEGER,
    PRIMARY KEY (evaluation_id, fact_id),
    FOREIGN KEY (snapshot_id, evaluation_id)
        REFERENCES report_evaluations(snapshot_id, id),
    FOREIGN KEY (snapshot_id, fact_id) REFERENCES facts(snapshot_id, id),
    FOREIGN KEY (snapshot_id, selected_fact_id) REFERENCES facts(snapshot_id, id)
);

CREATE TABLE fact_exclusion_reasons (
    evaluation_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    reason_order INTEGER NOT NULL CHECK (reason_order >= 0),
    reason_code TEXT NOT NULL,
    detail TEXT NOT NULL,
    PRIMARY KEY (evaluation_id, fact_id, reason_order),
    FOREIGN KEY (snapshot_id, evaluation_id)
        REFERENCES report_evaluations(snapshot_id, id),
    FOREIGN KEY (snapshot_id, fact_id) REFERENCES facts(snapshot_id, id)
);

CREATE TABLE reconciliation_issues (
    id INTEGER PRIMARY KEY,
    evaluation_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    concept_id INTEGER NOT NULL,
    reason_code TEXT NOT NULL,
    reason_text TEXT NOT NULL,
    issue_order INTEGER NOT NULL CHECK (issue_order >= 0),
    UNIQUE (evaluation_id, issue_order),
    UNIQUE (snapshot_id, id),
    FOREIGN KEY (snapshot_id, evaluation_id)
        REFERENCES report_evaluations(snapshot_id, id),
    FOREIGN KEY (snapshot_id, concept_id)
        REFERENCES snapshot_concepts(snapshot_id, concept_id)
);

CREATE TABLE reconciliation_issue_facts (
    issue_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    candidate_order INTEGER NOT NULL CHECK (candidate_order >= 0),
    PRIMARY KEY (issue_id, fact_id),
    UNIQUE (issue_id, candidate_order),
    FOREIGN KEY (snapshot_id, issue_id) REFERENCES reconciliation_issues(snapshot_id, id),
    FOREIGN KEY (snapshot_id, fact_id) REFERENCES facts(snapshot_id, id)
);

CREATE TABLE validation_messages (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    message_order INTEGER NOT NULL CHECK (message_order >= 0),
    level TEXT NOT NULL,
    code TEXT NOT NULL,
    message_text TEXT NOT NULL,
    raw_record_json TEXT NOT NULL,
    UNIQUE (snapshot_id, message_order),
    UNIQUE (snapshot_id, id)
);

CREATE TABLE validation_references (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    snapshot_id INTEGER NOT NULL,
    reference_order INTEGER NOT NULL CHECK (reference_order >= 0),
    raw_reference_json TEXT NOT NULL,
    resolution_status TEXT NOT NULL CHECK (
        resolution_status IN ('resolved', 'ambiguous', 'unresolved')
    ),
    fact_id INTEGER,
    source_document_id INTEGER,
    source_locator TEXT,
    UNIQUE (message_id, reference_order),
    FOREIGN KEY (snapshot_id, message_id) REFERENCES validation_messages(snapshot_id, id),
    FOREIGN KEY (snapshot_id, fact_id) REFERENCES facts(snapshot_id, id),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id)
);

CREATE TABLE roles (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    role_uri TEXT NOT NULL,
    definition TEXT,
    source_locator TEXT,
    UNIQUE (snapshot_id, role_uri),
    UNIQUE (snapshot_id, id)
);

CREATE TABLE relationship_networks (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    arcrole_uri TEXT NOT NULL,
    extraction_status TEXT NOT NULL CHECK (
        extraction_status IN ('extracted', 'extracted_empty', 'extraction_failed')
    ),
    relationship_count INTEGER NOT NULL CHECK (relationship_count >= 0),
    UNIQUE (snapshot_id, arcrole_uri),
    UNIQUE (snapshot_id, id)
);

CREATE TABLE calculation_arcs (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    network_id INTEGER NOT NULL,
    role_id INTEGER NOT NULL,
    relationship_order INTEGER NOT NULL CHECK (relationship_order >= 0),
    parent_concept_id INTEGER NOT NULL,
    child_concept_id INTEGER NOT NULL,
    exact_weight_text TEXT NOT NULL,
    exact_order_text TEXT,
    link_namespace_uri TEXT,
    link_local_name TEXT,
    arc_namespace_uri TEXT,
    arc_local_name TEXT,
    source_document_id INTEGER,
    source_locator TEXT,
    raw_arc_details_json TEXT,
    UNIQUE (snapshot_id, network_id, role_id, relationship_order),
    FOREIGN KEY (snapshot_id, network_id)
        REFERENCES relationship_networks(snapshot_id, id),
    FOREIGN KEY (snapshot_id, role_id) REFERENCES roles(snapshot_id, id),
    FOREIGN KEY (snapshot_id, parent_concept_id)
        REFERENCES snapshot_concepts(snapshot_id, concept_id),
    FOREIGN KEY (snapshot_id, child_concept_id)
        REFERENCES snapshot_concepts(snapshot_id, concept_id),
    FOREIGN KEY (snapshot_id, source_document_id)
        REFERENCES source_documents(snapshot_id, id)
);

CREATE INDEX filings_history_idx
    ON filings(company_id, form, report_date, accession);
CREATE INDEX evidence_snapshots_profile_idx
    ON evidence_snapshots(filing_id, extraction_profile_hash, id);
CREATE INDEX facts_concept_idx ON facts(snapshot_id, concept_id, source_order);
CREATE INDEX facts_context_idx ON facts(snapshot_id, context_id);
CREATE INDEX facts_unit_idx ON facts(snapshot_id, unit_id);
CREATE INDEX contexts_period_idx
    ON contexts(snapshot_id, period_kind, period_end_date, period_start_date);
CREATE INDEX context_dimensions_member_idx
    ON context_dimensions(
        snapshot_id, axis_namespace_uri, axis_local_name,
        explicit_member_namespace_uri, explicit_member_local_name
    );
CREATE INDEX fact_report_status_role_idx
    ON fact_report_status(evaluation_id, evidence_role, fact_id);
CREATE INDEX reconciliation_issue_facts_order_idx
    ON reconciliation_issue_facts(issue_id, candidate_order);
CREATE INDEX calculation_arcs_parent_idx
    ON calculation_arcs(snapshot_id, role_id, parent_concept_id);
CREATE INDEX validation_messages_order_idx
    ON validation_messages(snapshot_id, message_order);

CREATE VIEW fact_dimensions AS
SELECT
    f.id AS fact_id,
    f.snapshot_id,
    d.id AS dimension_id,
    d.source_order,
    d.axis_namespace_uri,
    d.axis_local_name,
    d.member_kind,
    d.context_element,
    d.explicit_member_namespace_uri,
    d.explicit_member_local_name,
    d.typed_xml,
    d.typed_text,
    d.typed_hash
FROM facts AS f
JOIN context_dimensions AS d
  ON d.snapshot_id = f.snapshot_id AND d.context_id = f.context_id;
