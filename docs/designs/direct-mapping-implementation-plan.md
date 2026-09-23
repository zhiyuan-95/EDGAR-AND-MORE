# Direct Mapping Design and Implementation Record

- **Status:** Implemented runtime; dedicated Direct Mapping regression tests are not present in the current checkout
- **Scope:** Backend-only Direct Mapping for the seven Target Metrics
- **Milestone:** Milestone 2 — Direct Mapping
- **Current implementation:** migration 0004, mapping modules, ingestion integration, and `sec-inline-financials-map`

## 1. Outcome

The system implements deterministic Direct Mapping over evidence already stored in SQLite.
For every active annual and quarterly filing period, the system evaluates all seven
Target Metrics and persists either a reported fact or an explicit missing result.

The seven Target Metrics are:

1. Revenue
2. Operating Income
3. Net Income
4. Total Assets
5. Total Liabilities
6. Equity
7. Operating Cash Flow

The implemented runtime:

- evaluate the latest five active 10-K snapshots and latest twelve active 10-Q
  snapshots when a complete 5/12 window is available;
- keep annual and quarterly concept populations separate;
- use configured primary and alternative concepts in a deterministic order;
- preserve reported zero as a reported result;
- persist one result for every Target Metric and supported filing period;
- link every reported result to the exact stored fact, snapshot, filing, and report
  evaluation that produced it;
- retain missing periods for the later Mapping Recommendation workflow;
- reuse a prior evaluation when its evidence window and rule versions are unchanged;
  and
- run entirely from stored evidence without SEC access, Arelle processing, report
  generation, or LLM calls.

For a complete 5/12 filing window, one full evaluation produces 119 metric-period
results: `7 * (5 annual + 12 quarterly)`.

## 2. Fixed design decisions

These decisions define the first Direct Mapping version.

1. **One evidence store.** Facts, concepts, contexts, report decisions, and metric
   results stay in the existing normalized SQLite database. Do not create annual and
   quarterly databases or duplicate concept catalogs.
2. **Two report-kind views.** Derive one observed-concept union from active 10-K
   snapshots and another from active 10-Q snapshots. Never let annual concept
   presence establish quarterly coverage, or the reverse.
3. **The union is an index, not the value source.** It tells the resolver whether a
   configured mapping appears anywhere in a report-kind window. Each metric value is
   still selected independently from the exact filing-period snapshot.
4. **Batch resolution.** Load candidate facts for all seven metrics in one bounded
   query per report kind, then resolve the small metric-period grid in memory. Do not
   scan all stored facts once per metric and period.
5. **Ordered fallback per period.** Use the primary concept when it has a selectable
   fact for the period. Otherwise use the first selectable configured alternative.
   Record the selected concept and rank on every reported cell so tag changes across
   filings remain visible.
6. **Two public result states.** Direct Mapping exposes only `reported` and `missing`
   in this milestone. Existing report conflict and exclusion details remain internal
   diagnostics and evidence.
7. **Two high-level missing reasons.** A missing result uses
   `mapping_not_found` or `no_selectable_fact_for_period`. More specific report
   exclusion and conflict codes belong in the resolution trace rather than the
   public status.
8. **No false missing results.** Missing or incomplete active evidence is a mapping
   run failure. It must not be converted into seven missing metric results.
9. **Independent policy version.** Direct Mapping uses `direct-mapping-v1`. It can
   consume stored `report-v1` decisions, but it must not store metric decisions in
   the report evaluation tables.
10. **Generic kernel.** The resolver has no metric-specific branches. Revenue uses
    the normal duration path, Operating Cash Flow exercises the quarterly-duration
    boundary, and the remaining metrics use the same rule-driven kernel.

## 3. Non-goals

This milestone does not implement:

- LLM candidate discovery or Mapping Recommendations;
- approval or rejection of recommended mappings;
- company-specific accepted mapping overrides;
- composite or derived metrics;
- Q4 derivation from annual and nine-month facts;
- year-to-date subtraction for quarterly cash-flow values;
- frontend pages, local HTTP endpoints, downloads, or background jobs;
- changes to Arelle extraction; or
- automatic TXT or JSON inspection-report generation.

The schema and service boundaries should leave room for accepted company-specific
rules and multi-fact results later, but those behaviors remain outside this plan.

## 4. Current implementation seams

Direct Mapping uses the following components.

| Existing component | Reuse in Direct Mapping |
|---|---|
| `docs/mapping.txt` | Human-readable requirements source for the seven primary and alternative concept names |
| `filings.is_active` and `active_window_rank` | Identify the current five-annual/twelve-quarter filing window |
| `evidence_snapshots` | Supply immutable filing-scoped evidence and payload identity |
| `concepts` and `snapshot_concepts` | Resolve namespace-qualified concepts and snapshot-specific metadata |
| `facts`, `contexts`, `units`, and `fact_dimensions` | Supply exact values, periods, units, entity identifiers, and dimensions |
| `report_evaluations` and `fact_report_status` | Supply persisted `report-v1` eligibility, duplicate selection, dimensional, conflict, and exclusion decisions |
| `fact_exclusion_reasons` and reconciliation tables | Explain why a configured candidate could not supply a value |
| `EvidenceStore` | Own all bounded reads, transactions, foreign-key checks, and persisted mapping results |
| `CompanyIngestionService` | Publish a new evidence window and trigger mapping refresh after storage succeeds |

Migration 0004 fills the two original gaps: `active_filing_snapshots` binds each
published filing to an exact immutable snapshot, and the mapping tables persist and
publish immutable metric evaluations. The rule model, resolver, service, CLI, and
ingestion integration live in the modules listed in section 13.

Do not use the rendered TXT report or complete-facts JSON export as an input. Those
files are inspection outputs. Direct Mapping reads the normalized database.

## 5. Target architecture

```text
checked-in direct-mapping-v1 rules
                 |
                 v
active 10-K filings -----> published filing/snapshot bindings
active 10-Q filings -----> published filing/snapshot bindings
                                  |
                                  v
                         stored report-v1 decisions
                                  |
                +-----------------+-----------------+
                |                                   |
                v                                   v
      annual observed/selectable union    quarterly observed/selectable union
                |                                   |
                +-----------------+-----------------+
                                  |
                                  v
                      batch candidate-fact query
                                  |
                                  v
                     direct-mapping-v1 resolver
                                  |
                +-----------------+-----------------+
                |                                   |
                v                                   v
        reported result + fact link       missing result + trace
                |                                   |
                +-----------------+-----------------+
                                  |
                                  v
                       atomic evaluation publish
```

The resolver is a pure deterministic layer over detached stored records. The
service and store own input selection, persistence, reuse, and publication.

## 6. Mapping rule model

### 6.1 Packaged runtime rules

Runtime code does not parse the numbered prose format in `docs/mapping.txt`. It is
not guaranteed to be present in an installed wheel and is easy to parse incorrectly.

`src/sec_inline_financials/mapping_rules.py` contains frozen typed rule objects and
these constants:

```python
DIRECT_MAPPING_RULE_VERSION = "direct-mapping-v1"
TARGET_METRIC_DEFINITION_VERSION = "target-metrics-v1"
```

Each `MetricRule` contains:

```text
metric_key
display_name
expected_period_type       duration | instant
expected_unit_family       monetary
candidates                 ordered ConceptCandidate tuple
```

Each `ConceptCandidate` contains:

```text
namespace_family           us-gaap for the initial seven metrics
local_name
rank                       0 for primary, then 1..N alternatives
```

Serialize the complete ordered rule set to canonical JSON and calculate its SHA-256.
Store both the JSON and hash on every mapping evaluation. Changing rule content
requires a new rule version; changing content under an existing version is an error.

### 6.2 Initial seven rules

| Metric | Period type | Ordered concepts |
|---|---|---|
| Revenue | duration | `RevenueFromContractWithCustomerExcludingAssessedTax`, `Revenues`, `SalesRevenueNet`, `RevenueFromContractWithCustomerIncludingAssessedTax` |
| Operating Income | duration | `OperatingIncomeLoss` |
| Net Income | duration | `NetIncomeLoss`, `ProfitLoss` |
| Total Assets | instant | `Assets` |
| Total Liabilities | instant | `Liabilities` |
| Equity | instant | `StockholdersEquity`, `StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest` |
| Operating Cash Flow | duration | `NetCashProvidedByUsedInOperatingActivities`, `NetCashProvidedByUsedInOperatingActivitiesContinuingOperations` |

All names above belong to the configured `us-gaap` namespace family. A match must
verify both namespace family and local name. Never match a company extension or an
unrelated namespace only because its local name or label is similar.

The namespace-family resolver must use explicit, tested URI rules. It must preserve
the exact namespace URI and display QName from the selected stored concept for
lineage. Labels are evidence for later recommendation, not Direct Mapping keys.

### 6.3 Rule validation

Reject the packaged rule set during initialization when:

- a metric key or display name is empty;
- the seven required metric keys are not present exactly once;
- candidate ranks are duplicated or not consecutive from zero;
- a candidate namespace family or local name is empty;
- the same concept candidate appears twice within one metric;
- an instant metric is configured with a duration-only rule, or the reverse; or
- the rule version or definition version is empty.

## 7. Active filing and snapshot selection

### 7.1 Persist the selected snapshot

Migration 0004 adds `active_filing_snapshots` rather than guessing with
`MAX(snapshot.id)`:

```text
active_filing_snapshots
  filing_id                primary key
  snapshot_id              required
  published_at             required UTC timestamp
```

Enforce `(snapshot_id, filing_id)` as a foreign key to
`evidence_snapshots(id, filing_id)`. The table is a mutable pointer to current
evidence, not a replacement for immutable snapshots.

`EvidenceStore.publish_filing_window` uses the same transaction that publishes
the active filing ranks also publishes each successful or reused filing's exact
snapshot ID. Remove bindings for filings leaving the active window. Leave an active
filing without a binding when its current processing attempt failed; mapping
preflight then refuses to publish a new evaluation.

Migration 0004 backfills existing active filings from their latest successful
`processing_run_filings` record with a non-null snapshot ID. If no unambiguous
successful snapshot exists, leave the binding absent and require an evidence refresh
before mapping.

### 7.2 Mapping preflight

For the requested company and report kind:

1. Load active exact-form filings ordered by `active_window_rank`.
2. Require the configured filing count when the caller requests a complete-window
   evaluation.
3. Require one published snapshot binding for every selected filing.
4. Require one `report-v1` evaluation of the matching report kind for every selected
   snapshot.
5. Require each selected snapshot and its linked artifacts to pass the existing
   integrity boundary when the mapping call follows a reuse path.

If any requirement fails, return a mapping error and leave the last published metric
evaluation unchanged. Do not write missing metric results for unavailable evidence.

## 8. Annual and quarterly concept unions

Build the unions as bounded queries over the selected active snapshots. They do not
need permanent tables.

For each report kind, derive:

1. **Observed fact concept union:** distinct concepts referenced by stored fact
   occurrences. This determines whether a configured mapping appeared anywhere in
   the active annual or quarterly window.
2. **Selectable concept union:** distinct concepts whose facts have
   `fact_report_status.evidence_role = 'selected_primary'` under the required
   `report-v1` evaluations. This prunes the value-selection query.

Do not build the observed union from every `snapshot_concepts` row. That table also
contains concepts used only by dimensions or calculation relationships. A concept
definition without a fact occurrence does not establish a Direct Mapping match.

The annual query may use only snapshots bound to active exact-form 10-K filings. The
quarterly query may use only snapshots bound to active exact-form 10-Q filings.

## 9. Fact eligibility for Direct Mapping

`direct-mapping-v1` consumes `report-v1` selected-primary facts rather than
reimplementing its period, numeric, validity, dimensional, duplicate, and conflict
logic. Record the exact source report evaluation ID for every metric-period result.

The resolver applies these mapping-specific checks after the report decision:

- the concept matches the configured namespace family and local name;
- the concept's period type matches the Target Metric rule;
- the context entity identifier matches the selected company after a documented CIK
  normalization rule;
- the unit resolves to the configured monetary unit family;
- the context ends on the filing report date; and
- the stored typed numeric text parses as a finite `Decimal`.

The report layer already checks several of these conditions. Rechecking the
mapping-specific contract protects the metric result if a later report rule has a
different presentation purpose.

Do not store a floating-point copy of the value. The selected immutable fact retains
the exact raw and typed text, decimals, unit, context, filing, and source locator.

## 10. Resolution algorithm

Run one batch for annual and one for quarterly.

```text
validate rule set
load and validate active filing/snapshot/report-evaluation inputs
derive observed and selectable concept unions
fetch all selectable facts matching any configured candidate
group rows by (snapshot_id, metric_key, candidate_rank)

for each metric in the seven-metric set:
    mapping_seen = any configured candidate in the observed union

    for each selected snapshot in active-window order:
        selected = first selectable candidate ordered by rank

        if selected exists:
            persist reported
            link the selected fact
            retain selected candidate rank and resolution trace
        else if mapping_seen is false:
            persist missing / mapping_not_found
        else:
            persist missing / no_selectable_fact_for_period
```

### 10.1 Fallback rules

- Rank zero is the primary concept.
- An alternative may fill a period only when no higher-ranked candidate has a
  selectable fact for that period.
- A reported numeric zero is selectable and stops fallback.
- Never combine two candidate concepts in one Direct Mapping result.
- Never sum dimensional members, average conflicting facts, or calculate a missing
  value.
- If an alternative is used, persist its exact rank and concept through the selected
  fact link.
- If configured concepts change across periods, keep the per-cell lineage. Do not
  rewrite older periods to force one QName across the whole window.

### 10.2 Missing semantics

`mapping_not_found` means none of the configured primary or alternative concepts
has any fact occurrence in the relevant annual or quarterly active window.

`no_selectable_fact_for_period` means at least one configured concept occurs
somewhere in that report-kind window, but the current period has no fact that passes
all report and mapping checks.

Persist a canonical `resolution_trace_json` for both statuses. It should include the
ordered candidate names, observed/selected presence flags, selected rank when
reported, and relevant existing exclusion or conflict reason codes. This trace does
not create more public result states.

## 11. Persistence design

`src/sec_inline_financials/storage/sql/0004_direct_mapping.sql` creates the following
tables.

### 11.1 `active_filing_snapshots`

Stores the exact current snapshot binding described in Section 7.

### 11.2 `target_metrics`

```text
id
metric_key
display_name
definition_version
expected_period_type
expected_unit_family
created_at
```

Use a unique key on `(metric_key, definition_version)`. Seed the seven
`target-metrics-v1` definitions deterministically.

### 11.3 `metric_evaluations`

```text
id
company_id
report_kind                 annual | quarterly
definition_version
mapping_rule_version
mapping_rule_hash
mapping_rule_json
source_report_rule_version
active_window_hash
evaluated_at
```

The active-window hash is canonical JSON over the ordered input identities:

```text
form
active_window_rank
filing_id
snapshot_id
snapshot_payload_hash
report_evaluation_id
```

The schema has a unique key over company, report kind, definition version, mapping rule hash,
source report rule version, and active-window hash. Reuse an existing matching
evaluation rather than inserting duplicate results.

### 11.4 `metric_results`

```text
id
evaluation_id
target_metric_id
snapshot_id
report_evaluation_id
status                       reported | missing
missing_reason               null | mapping_not_found |
                             no_selectable_fact_for_period
selected_candidate_rank      nullable nonnegative integer
resolution_trace_json
```

Use a unique key on `(evaluation_id, target_metric_id, snapshot_id)`.

Database checks must enforce:

- `reported` has no missing reason and has a selected candidate rank;
- `missing` has an allowed missing reason and no selected candidate rank; and
- the report evaluation belongs to the same snapshot as the metric result.

### 11.5 `metric_result_facts`

```text
metric_result_id
snapshot_id
fact_id
role                         selected
```

Use composite foreign keys so a result cannot link a fact from another snapshot.
Allow only one `selected` fact for a Direct Mapping result. Keeping the link table
instead of a comma-separated fact ID leaves a clean path for later approved
multi-fact mappings.

Database checks or deferred validation must enforce:

- every `reported` result has exactly one selected fact link; and
- every `missing` result has no selected fact link.

### 11.6 `published_metric_evaluations`

```text
company_id
report_kind
evaluation_id
published_at
```

Use `(company_id, report_kind)` as the primary key. Enforce that the referenced
evaluation belongs to the same company and report kind. Updating this pointer does
not modify the immutable evaluation or its results.

## 12. Transaction and publication protocol

Use the same immutable-evaluation pattern as stored report decisions.

1. Read and validate the current active snapshot bindings.
2. Build the two report-kind input hashes.
3. Reuse already persisted evaluations when all versions and hashes match.
4. Otherwise compute the full report-kind result set outside the write transaction.
5. Start one short `BEGIN IMMEDIATE` transaction.
6. Re-read and verify that the active snapshot bindings and report evaluations still
   match the captured input hash.
7. Insert the complete evaluation, all metric results, traces, and fact links.
8. Verify result cardinality and reported/missing link invariants.
9. Update the published evaluation pointer.
10. Commit.

Any exception before commit rolls back the new evaluation and leaves the previous
published evaluation available. A mapping failure must not roll back an already
committed evidence ingestion or active filing window.

A reader must compare the published evaluation's active-window hash with the current
active snapshot bindings. If they differ, expose the prior evaluation as stale rather
than presenting its values as current for the new filing window.

## 13. Python modules and interfaces

The implementation uses these modules:

```text
src/sec_inline_financials/mapping_models.py
src/sec_inline_financials/mapping_rules.py
src/sec_inline_financials/direct_mapping.py
src/sec_inline_financials/mapping_service.py
```

Current interfaces:

```python
DIRECT_MAPPING_RULES: DirectMappingRuleSet

resolve_metric_window(
    *,
    rule_set: DirectMappingRuleSet,
    report_kind: ReportKind,
    snapshots: tuple[MappingSnapshotInput, ...],
    observed_concepts: frozenset[ConceptIdentity],
    candidate_facts: tuple[MappingFactInput, ...],
) -> MetricWindowEvaluation

DirectMappingService.evaluate_company(
    ticker: str,
    *,
    require_complete_window: bool = True,
) -> CompanyMappingResult
```

`EvidenceStore` owns the focused SQL methods instead of embedding SQL in the resolver:

```text
list_mapping_inputs(ticker, report_kind, report_rule_version)
load_mapping_candidate_facts(input_snapshots, concept_candidates)
find_metric_evaluation(input_hashes and rule versions)
save_and_publish_metric_evaluation(evaluation)
get_published_metric_evaluation(ticker, report_kind)
```

The pure resolver must not open SQLite, access the filesystem, contact the SEC, open
Arelle, or render reports.

## 14. Ingestion and refresh integration

Mapping is integrated after evidence publication and remains a separate failure
boundary.

1. Active-window publication stores successful snapshot bindings.
2. After a new active window commits, ingestion runs Direct Mapping for annual and quarterly.
3. On `reused_local`, ingestion calls the mapping service; the service reuses the
   immutable evaluation when its rule and active-window identities match.
4. On `checked_no_update`, the service likewise reuses the matching evaluation.
5. On `updated`, the service creates and publishes new annual and quarterly evaluations for the
   changed window. Recomputing at most 119 cells is simpler and safer than partial
   in-place mutation.
6. On `refresh_failed_using_local_data`, ingestion leaves the last published evidence
   and metric-evaluation pointers unchanged; it does not start a new mapping run.
7. If mapping fails after evidence publication, preserve the new evidence window,
   retain the previous published metric evaluation as stale, and surface a separate
   mapping error. Do not present the stale values as current and do not change the
   meaning of existing ingestion statuses.

Mapping progress is written to stderr. Existing ingestion summary lines remain on
stdout.

For direct manual verification, use the stored-evidence-only command:

```powershell
uv run --no-sync sec-inline-financials-map AAPL
```

The command prints annual and quarterly evaluation IDs plus reported/missing counts.
It does not contact the SEC, invoke Arelle, or generate a report.

## 15. Historical implementation work packages

The packages below preserve the approved delivery sequence. Their source modules
exist, but the dedicated mapping test files named in this historical plan were later
removed from the checkout. Treat their test lists as required regression coverage,
not as a description of the active suite.

### P0. Freeze contracts with tests

Files:

- `tests/test_mapping_rules.py`
- `tests/test_direct_mapping.py`

Work:

- Write failing tests for the seven rules, stable rule hashing, namespace matching,
  primary precedence, alternative fallback, reported zero, the two missing reasons,
  and annual/quarterly isolation.
- Add Revenue fixtures for the normal duration path.
- Add Operating Cash Flow fixtures for a discrete quarter, a six-month YTD fact, and
  a missing discrete quarter.

Exit:

- Tests express the complete resolver contract and fail because the mapping modules
  do not exist.

### P1. Add typed and versioned mapping rules

Files:

- `src/sec_inline_financials/mapping_models.py`
- `src/sec_inline_financials/mapping_rules.py`
- `tests/test_mapping_rules.py`

Work:

- Implement frozen rule dataclasses, validation, namespace-family matching,
  canonical serialization, and hashing.
- Transcribe only the seven Target Metric rules from `docs/mapping.txt`.

Exit:

- Invalid or silently changed rules fail fast; valid `direct-mapping-v1` rules have a
  stable canonical hash.

### P2. Bind active filings to exact snapshots

Files:

- `src/sec_inline_financials/storage/sql/0004_direct_mapping.sql`
- `src/sec_inline_financials/storage/evidence_store.py`
- `src/sec_inline_financials/company_ingestion.py`
- `tests/test_mapping_storage.py`

Work:

- Add and backfill `active_filing_snapshots`.
- Carry snapshot IDs from per-filing ingestion outcomes into active-window
  publication.
- Reject cross-filing snapshot links and expose incomplete bindings explicitly.

Exit:

- Every successfully published active filing points to its intended immutable
  snapshot, and failed/incomplete bindings cannot enter mapping evaluation.

### P3. Persist immutable mapping evaluations

Files:

- `src/sec_inline_financials/storage/sql/0004_direct_mapping.sql`
- `src/sec_inline_financials/storage/evidence_store.py`
- `tests/test_mapping_storage.py`

Work:

- Add target metrics, evaluations, results, result-fact links, publication pointers,
  constraints, and indexes.
- Implement idempotent lookup and atomic save/publish operations.
- Inject failures at several insert points and prove no partial evaluation becomes
  visible.

Exit:

- Repeating the same input returns the same evaluation; changed rules or input
  windows create a new immutable evaluation; failed writes preserve the prior
  published evaluation.

### P4. Implement the Revenue and Operating Cash Flow kernel

Files:

- `src/sec_inline_financials/direct_mapping.py`
- `src/sec_inline_financials/mapping_service.py`
- `src/sec_inline_financials/storage/evidence_store.py`
- `tests/test_direct_mapping.py`
- `tests/test_mapping_service.py`

Work:

- Implement the observed/selectable unions and one batch candidate query per report
  kind.
- Implement per-period ordered resolution, trace construction, and exact fact
  linking.
- Verify Revenue for annual and quarterly duration contexts.
- Verify Operating Cash Flow accepts discrete-quarter facts and rejects six-/nine-
  month YTD values without deriving replacements.

Exit:

- The generic resolver produces correct annual and quarterly results for both kernel
  metrics without report rendering or external access.

### P5. Enable the remaining five metrics

Files:

- `src/sec_inline_financials/mapping_rules.py`
- `tests/test_direct_mapping.py`
- `tests/test_mapping_service.py`

Work:

- Enable Operating Income, Net Income, Total Assets, Total Liabilities, and Equity.
- Add instant-period, namespace, unit, alternative-use, and tag-switch tests.
- Assert exactly seven results per selected snapshot.

Exit:

- A complete 5/12 window persists exactly 119 results with complete lineage or an
  explicit missing reason.

### P6. Integrate mapping with company access and refresh

Files:

- `src/sec_inline_financials/company_ingestion.py`
- `src/sec_inline_financials/cli.py`
- `pyproject.toml`
- `tests/test_mapping_integration.py`

Work:

- Add the stored-evidence mapping command.
- Evaluate or reuse mappings after company access and successful window publication.
- Preserve evidence and the previous published metric evaluation when mapping fails.
- Keep report generation separate.

Exit:

- Initialized, reused, checked-no-update, updated, partial, and refresh-fallback paths
  have explicit mapping behavior and do not change existing ingestion status meaning.

### P7. Verify and align documentation

Files:

- `README.md`
- `CONTEXT.md`
- `docs/designs/project_proposal.txt`
- focused Direct Mapping documentation

Work:

- Document the new command, statuses, rule version, evidence links, and verification
  boundary.
- Resolve the old Milestone 2/Milestone 3 numbering mismatch by naming the feature
  Direct Mapping consistently.
- Replace promises of extra public metric states with the implemented two-state
  contract while documenting internal diagnostics.

Exit:

- Current docs match implemented behavior and do not imply LLM, frontend, Q4, or YTD
  derivation support.

## 16. Required Direct Mapping test matrix

This matrix remains the intended contract. The current `tests/` directory does not
contain the dedicated rule, resolver, storage, service, or integration test modules
listed in section 15, so `pytest -q` does not currently re-prove these cases.

### Rule tests

- Exactly seven Target Metrics exist.
- Candidate order is stable and deterministic.
- Duplicate keys, duplicate ranks, missing rank zero, and empty names fail.
- Rule serialization and hash are stable across runs.
- A same-local-name concept from the wrong namespace does not match.

### Resolver tests

- Primary fact wins when primary and alternatives are available.
- First available alternative fills only periods missing a selectable primary.
- A reported zero stops fallback and remains reported.
- Annual candidate presence cannot satisfy a quarterly cell.
- Quarterly candidate presence cannot satisfy an annual cell.
- A concept absent from the report-kind observed union produces
  `mapping_not_found`.
- A concept seen elsewhere in the union but unavailable for one period produces
  `no_selectable_fact_for_period`.
- Invalid, nil, dimensional-only, and conflicting candidates do not become reported
  values and remain visible in the resolution trace.
- Concept switching across periods retains the selected fact and rank per cell.
- OCF six-/nine-month YTD facts do not fill discrete-quarter cells.
- No Q4 result is manufactured.

### Storage tests

- Migration 0004 applies once, records its checksum, and rolls back on failure.
- Existing active filings backfill only unambiguous successful snapshot bindings.
- Cross-filing and cross-snapshot links fail through SQLite foreign keys.
- Reported and missing check constraints reject invalid combinations.
- A reported result has exactly one selected fact link.
- A missing result has no selected fact link.
- Same window and rules reuse the same evaluation.
- A new window or rule hash creates a new evaluation without changing the old one.
- Mid-save failures leave no partial evaluation or publication pointer.

### Service and integration tests

- One bounded candidate load occurs per report kind, not once per cell.
- Complete 5/12 evidence produces 119 results.
- Incomplete active evidence publishes no new metric evaluation.
- `reused_local` creates a missing evaluation when needed and otherwise reuses it.
- `checked_no_update` does not duplicate evaluations.
- `updated` publishes a new evaluation after the new evidence window is active.
- Mapping failure leaves evidence ingestion committed and the previous metric
  evaluation published.
- The mapping command constructs neither an SEC gateway nor an Arelle processor.
- Mapping creates no TXT or complete-facts JSON report.

## 17. Performance and query constraints

The window is intentionally small, so prefer simple complete recomputation over
partial mutable updates. The performance goal is bounded work, not clever caching.

- Resolve all configured concept identities once per rule set.
- Run one candidate-fact query per report kind.
- Use the existing active-window, fact-concept, context-period, and report-role
  indexes.
- Add indexes for published snapshot lookup, evaluation reuse, metric-result grid
  lookup, and result-fact traversal.
- Group at most the candidate rows for seven metrics in Python.
- Insert one complete evaluation in one transaction.
- Never load all facts into Python and never parse report text or exported JSON.
- Do not sort or aggregate exact numeric text in SQLite.

## 18. Verification commands

Run the current repository checks:

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy src
```

Run the stored-evidence acceptance against an explicitly selected local store:

```powershell
uv run --no-sync sec-inline-financials-map AAPL
```

The acceptance record must state whether it used an existing local 5/12 window or a
synthetic fixture. An offline stored-evidence run is not proof that SEC access or a
new-accession refresh occurred.

## 19. Implementation status and acceptance boundary

The runtime implements the following design conditions:

- the runtime rule set contains exactly the seven Target Metrics and has a stable
  version and hash;
- active filings point to explicit immutable snapshots;
- annual and quarterly unions are independently derived from those snapshots;
- all seven metrics are evaluated for every selected filing period in two bounded
  batches;
- every result is `reported` or `missing`;
- every reported result links exactly one selected stored fact and preserves zero;
- every missing result records one high-level reason and a detailed trace;
- a complete 5/12 window contains exactly 119 results;
- repeated identical evaluation is idempotent;
- new windows or rule versions create new immutable evaluations;
- failed mapping never replaces the previous published evaluation or rolls back
  stored evidence;
- no mapping path contacts the SEC, invokes Arelle, calls an LLM, or generates a
  report;
- the stored-evidence-only acceptance result, when run, is reported with its exact
  scope and limitations.

The current verification gap is automated regression coverage. The active suite
does not contain the dedicated mapping tests described in sections 15 and 16.
Historical stored-evidence acceptance supports the implementation record, while the
interactive `tests/inspect_direct_mapping.py` remains a manual read-only inspector;
neither substitutes for restoring those focused tests.

## 20. Recommended commit sequence

Keep implementation reviewable with small commits:

1. `test: define direct mapping rule and resolver contracts`
2. `feat: add versioned seven-metric mapping rules`
3. `feat: bind active filings to exact evidence snapshots`
4. `feat: persist immutable metric evaluations`
5. `feat: resolve revenue and operating cash flow`
6. `feat: enable all seven direct mappings`
7. `feat: refresh metric evaluations after evidence updates`
8. `docs: align direct mapping behavior and verification`

Do not combine schema, resolver, ingestion integration, and documentation into one
commit. Each commit should leave the offline suite passing before the next layer is
added.
