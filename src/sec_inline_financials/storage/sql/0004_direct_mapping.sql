CREATE TABLE active_filing_snapshots (
    filing_id INTEGER PRIMARY KEY REFERENCES filings(id),
    snapshot_id INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    FOREIGN KEY (snapshot_id, filing_id) REFERENCES evidence_snapshots(id, filing_id)
);

INSERT INTO active_filing_snapshots(filing_id, snapshot_id, published_at)
SELECT f.id, prf.snapshot_id, CURRENT_TIMESTAMP
FROM filings AS f
JOIN processing_run_filings AS prf
  ON prf.id = (
      SELECT candidate.id
      FROM processing_run_filings AS candidate
      WHERE candidate.filing_id = f.id
        AND candidate.status IN ('stored', 'reused')
        AND candidate.snapshot_id IS NOT NULL
      ORDER BY candidate.completed_at DESC, candidate.id DESC
      LIMIT 1
  )
WHERE f.is_active = 1;

CREATE TABLE target_metrics (
    id INTEGER PRIMARY KEY,
    metric_key TEXT NOT NULL,
    display_name TEXT NOT NULL,
    definition_version TEXT NOT NULL,
    expected_period_type TEXT NOT NULL CHECK (expected_period_type IN ('duration', 'instant')),
    expected_unit_family TEXT NOT NULL CHECK (expected_unit_family = 'monetary'),
    created_at TEXT NOT NULL,
    UNIQUE (metric_key, definition_version),
    UNIQUE (id, definition_version)
);

INSERT INTO target_metrics(
    id, metric_key, display_name, definition_version,
    expected_period_type, expected_unit_family, created_at
) VALUES
    (1, 'revenue', 'Revenue', 'target-metrics-v1', 'duration', 'monetary', CURRENT_TIMESTAMP),
    (2, 'operating_income', 'Operating Income', 'target-metrics-v1', 'duration', 'monetary', CURRENT_TIMESTAMP),
    (3, 'net_income', 'Net Income', 'target-metrics-v1', 'duration', 'monetary', CURRENT_TIMESTAMP),
    (4, 'total_assets', 'Total Assets', 'target-metrics-v1', 'instant', 'monetary', CURRENT_TIMESTAMP),
    (5, 'total_liabilities', 'Total Liabilities', 'target-metrics-v1', 'instant', 'monetary', CURRENT_TIMESTAMP),
    (6, 'equity', 'Equity', 'target-metrics-v1', 'instant', 'monetary', CURRENT_TIMESTAMP),
    (7, 'operating_cash_flow', 'Operating Cash Flow', 'target-metrics-v1', 'duration', 'monetary', CURRENT_TIMESTAMP);

CREATE TABLE metric_evaluations (
    id INTEGER PRIMARY KEY,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    report_kind TEXT NOT NULL CHECK (report_kind IN ('annual', 'quarterly')),
    definition_version TEXT NOT NULL,
    mapping_rule_version TEXT NOT NULL,
    mapping_rule_hash TEXT NOT NULL CHECK (length(mapping_rule_hash) = 64),
    mapping_rule_json TEXT NOT NULL,
    source_report_rule_version TEXT NOT NULL,
    active_window_hash TEXT NOT NULL CHECK (length(active_window_hash) = 64),
    evaluated_at TEXT NOT NULL,
    UNIQUE (
        company_id, report_kind, definition_version, mapping_rule_hash,
        source_report_rule_version, active_window_hash
    ),
    UNIQUE (id, company_id, report_kind)
);

CREATE TRIGGER metric_evaluations_rule_identity_insert
BEFORE INSERT ON metric_evaluations
WHEN EXISTS (
    SELECT 1 FROM metric_evaluations AS prior
    WHERE prior.mapping_rule_version = NEW.mapping_rule_version
      AND (
          prior.mapping_rule_hash <> NEW.mapping_rule_hash
          OR prior.mapping_rule_json <> NEW.mapping_rule_json
      )
)
BEGIN
    SELECT RAISE(ABORT, 'mapping rule version content changed');
END;

CREATE TABLE metric_results (
    id INTEGER PRIMARY KEY,
    evaluation_id INTEGER NOT NULL REFERENCES metric_evaluations(id),
    target_metric_id INTEGER NOT NULL REFERENCES target_metrics(id),
    snapshot_id INTEGER NOT NULL REFERENCES evidence_snapshots(id),
    report_evaluation_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('reported', 'missing')),
    missing_reason TEXT CHECK (
        missing_reason IN ('mapping_not_found', 'no_selectable_fact_for_period')
        OR missing_reason IS NULL
    ),
    selected_candidate_rank INTEGER CHECK (
        selected_candidate_rank IS NULL OR selected_candidate_rank >= 0
    ),
    resolution_trace_json TEXT NOT NULL,
    UNIQUE (evaluation_id, target_metric_id, snapshot_id),
    UNIQUE (id, snapshot_id),
    CHECK (
        (status = 'reported' AND missing_reason IS NULL AND selected_candidate_rank IS NOT NULL)
        OR
        (status = 'missing' AND missing_reason IS NOT NULL AND selected_candidate_rank IS NULL)
    ),
    FOREIGN KEY (snapshot_id, report_evaluation_id)
        REFERENCES report_evaluations(snapshot_id, id)
);

CREATE TABLE metric_result_facts (
    metric_result_id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL,
    fact_id INTEGER NOT NULL,
    role TEXT NOT NULL CHECK (role = 'selected'),
    FOREIGN KEY (metric_result_id, snapshot_id)
        REFERENCES metric_results(id, snapshot_id),
    FOREIGN KEY (snapshot_id, fact_id) REFERENCES facts(snapshot_id, id)
);

CREATE TABLE published_metric_evaluations (
    company_id INTEGER NOT NULL,
    report_kind TEXT NOT NULL CHECK (report_kind IN ('annual', 'quarterly')),
    evaluation_id INTEGER NOT NULL,
    published_at TEXT NOT NULL,
    PRIMARY KEY (company_id, report_kind),
    FOREIGN KEY (evaluation_id, company_id, report_kind)
        REFERENCES metric_evaluations(id, company_id, report_kind)
);

CREATE INDEX active_filing_snapshots_snapshot_idx
    ON active_filing_snapshots(snapshot_id, filing_id);
CREATE INDEX metric_evaluations_window_idx
    ON metric_evaluations(company_id, report_kind, active_window_hash);
CREATE INDEX metric_results_grid_idx
    ON metric_results(evaluation_id, snapshot_id, target_metric_id);
CREATE INDEX metric_result_facts_fact_idx
    ON metric_result_facts(snapshot_id, fact_id);
