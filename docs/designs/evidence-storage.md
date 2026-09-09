# Section 3.1: Evidence Storage Implementation Plan

Date: 2026-09-08  
Status: IMPLEMENTED; local automated acceptance passed, opt-in live acceptance pending  
Basis: `docs/project_proposal.txt`, especially sections 2.2, 3.1, and Milestone 1  
Design choice: fresh plan; stored-evidence replay, selected by the project owner  
Reviewed checkout at drafting: branch `gstack`

## 1. Outcome and scope

Build a durable evidence store that can save a selected SEC filing, close Arelle,
restart the application, and retrieve the same observations and supporting
evidence without SEC requests or another Arelle run.

The first delivery implements Milestone 1. It stores all observed filing facts,
their meanings, contexts, units, dimensions, report classifications, conflict
candidates, validation evidence, calculation relationships, and source-file
provenance in one SQLite database with retained files alongside it.

The selected approach is stored-evidence replay. Full offline Arelle reprocessing
was considered and deferred: that would additionally require a complete dependency
archive, URL resolution, and an independently tested offline execution mechanism.
Reopening stored results is the acceptance criterion here.

The existing inspection-report command and its SEC/Arelle workflow stay separate.
Storage ingestion never writes TXT reports. Tests may explicitly pass a database
projection to the existing renderer to prove equivalence; this does not change
the production report command into a database client.

### Scope across the proposal's milestones

| Area from section 3.1 | Delivery in this plan |
|---|---|
| Companies, filings, processing runs | Implement in Milestone 1 |
| Concepts, contexts, units, every observed fact | Implement in Milestone 1 |
| Dimensions, conflicts, validation, calculations | Implement in Milestone 1 |
| Files, paths, hashes, source versions | Implement in Milestone 1 |
| Metric results and their fact links | Define the storage boundary here; implement with Milestone 2 |
| Recommendations, exact model packets/responses, review decisions | Define the storage boundary here; implement with Milestone 3 |
| Company refresh, amendment precedence, invalidation | Milestone 4; do not embed these policies in the evidence repository |
| HTTP download endpoints and frontend | Milestone 5; expose Python retrieval functions now |

This is a storage design, not authorization to implement mapping rules, quarter
derivation, a scheduler, a frontend, or a new financial-metric policy.

## 2. Current code and the required changes

The code inspection found these seams:

| Current code | Current behavior | Storage requirement |
|---|---|---|
| `arelle_adapter.py::_numeric_facts` | Drops nonnumeric, nil, invalid, missing-context/unit and non-report-period observations | Add complete extraction before any eligibility decisions |
| `models.py::Fact` | Requires a `Decimal`, a unit, and a period end | Keep this report model; add a separate observation model |
| `arelle_adapter.py::_process` | Raises when no selected primary or dimensional report facts remain | Storage can succeed with zero report-eligible facts if extraction itself completed |
| `reconcile.py::reconcile_primary_facts` | Collapses exact report duplicates; records conflicts using context IDs and summary text | Retain every occurrence and link selections/conflicts to permanent fact IDs |
| Validation extraction | Keeps level, code, and normalized message text from warning-level logs | Retain the raw log output, configured log policy, and structured references |
| Calculation extraction | Returns a deduplicated set of role/parent/child/weight tuples | Preserve supported effective relationship occurrences and their source provenance |
| `service.py::ReportApplication` | Produces annual/quarterly reports from in-memory results | Preserve this workflow; add a separate evidence-ingestion service |
| `report.py` | Formats detached read models and assigns E/D/R labels | Reuse in compatibility tests; E/D/R never become database keys |

The locked Arelle package is 2.41.7. Its `model.facts` contains top-level facts;
`factsInInstance` includes nested facts and is implemented as a set. Complete
extraction must not simply enumerate `model.facts` or depend on set iteration
order. See the [Arelle model documentation](https://arelle.readthedocs.io/en/latest/apidocs/arelle/arelle.ModelXbrl.html)
and verify behavior against the installed version during implementation.

## 3. Data flow and module responsibilities

```text
Explicit evidence-ingestion request
  -> existing SEC company/filing discovery
  -> inspect compatible stored snapshots
  -> for each missing filing:
       Arelle session
         -> detach every observation and supporting record
         -> capture the exact required source files
       close session
         -> classify report eligibility without deleting observations
         -> verify and install retained files
         -> commit one complete evidence snapshot
  -> return stored/reused/failed counts; no TXT report

Later, including after restart:
  snapshot ID -> SQLite -> detached evidence / query results
              -> retained artifact ID -> verified local file
```

Proposed package additions:

```text
src/sec_inline_financials/
  evidence_models.py          Detached records and result types
  evidence_extraction.py      Arelle-object to detached-record conversion
  evidence_classification.py  Existing report eligibility and selection semantics
  evidence_ingestion.py       One-filing and explicit filing-window orchestration
  storage/
    database.py              Connections and transaction ownership
    migrations.py            Numbered migration runner
    sql/0001_evidence.sql     Initial schema, constraints, indexes, views
    artifacts.py             Stage, verify, install and resolve retained files
    evidence_store.py        Save/load snapshots and bounded evidence queries
    report_projection.py     Explicit adapter used for report equivalence checks
    recovery.py              Reconcile interrupted attempts and artifact state
```

`ArelleProcessor` gains an `extract_evidence` entry point. Share only session setup
and proven extraction helpers with the current report path. Do not make existing
report processing depend on the new database. Keep the SEC and processor seams
injectable; repository tests should use a real temporary SQLite database.

Use the standard-library `sqlite3`, `decimal`, `hashlib`, and `pathlib` modules.
No ORM, database server, vector database, or job-queue dependency is required.

## 4. Evidence vocabulary and invariants

A concept says what a value means. A fact is one occurrence of that value in a
filing. A context says which entity, dates, and dimensions the occurrence covers.
A snapshot is one immutable, complete extraction of one filing under a recorded
extraction profile. A processing attempt records the work, including failure.

Example: a Q2 filing contains cash flow for January through June and a comparative
prior-year value. Both are facts and both must be stored. The current quarterly
report rejects the six-month duration, so its classification records that reason.
The database does not delete the fact or manufacture a three-month cash-flow value.

Required invariants:

1. Every fact object observed by Arelle receives an occurrence record. Exact
   duplicates remain separate. Report filtering never changes the occurrence set.
2. An invalid or unresolved observation may have missing links or typed values;
   its raw references, location, and extraction diagnostics remain available.
3. Numeric values and calculation weights use exact text, never SQLite `REAL`.
4. Zero, nil, invalid, empty text, and absence are distinct. An absent fact does
   not create a synthetic zero or a synthetic fact row.
5. Database IDs are permanent within the database and its backups. XML IDs,
   tickers, labels, and report E/D/R references are not substitutes for them.
6. A snapshot has one filing owner. Its facts cannot reference contexts, units,
   definitions, or candidate links from another snapshot.
7. A complete snapshot is immutable. Later extraction profiles can add snapshots;
   they cannot rewrite earlier observations or their evidence links.
8. Every complete snapshot references verified required artifacts. SQL rollback
   and filesystem recovery are separate responsibilities.
9. Arelle validation, report eligibility, duplicate/conflict selection, and future
   metric acceptance are independent records and concepts.
10. Dimension-free does not automatically mean the correct consolidated entity
    scope. Preserve entity identifiers and context content for later mapping rules.

## 5. Detached extraction contract

`FilingEvidenceBundle` contains filing/company metadata, source-document records,
concept definitions and labels, contexts, units, observations, validation records,
calculation relationships, extraction diagnostics, and an extraction profile.
It contains primitive values and immutable collections, with no live Arelle objects.

### 5.1 Observation coverage and identity

Traverse top-level facts recursively through tuple children, cross-check the result
against `factsInInstance`, and include any remaining instance facts. Also preserve
objects exposed through Arelle's `undefinedFacts` as unresolved observations with
their original names and locations. Deduplicate this traversal by object identity
only, so visiting the same object twice does not create two rows.

Assign `source_order` deterministically using source-document order and XML node
order. Preserve the original top-level Arelle order separately for compatibility
with existing selection and dimensional-evidence ordering. Retain tuple-parent
links. A source locator includes the original document URI and an XML node path;
XML ID and line number are supplemental because either may be absent or repeated.

The coverage manifest records recognized fact count, unresolved-observation count,
numeric/nonnumeric/nil/invalid counts, and supporting-record counts. A traversal or
serialization exception cannot silently reduce those counts and still succeed.

This promise covers observations Arelle exposes. It is not a claim to parse every
arbitrary malformed HTML element or every unrelated SEC attachment as an XBRL fact.

### 5.2 Values and source fidelity

For each observation retain:

- Concept expanded name, display QName, and resolved concept link if available.
- Arelle's source-value text, typed-value kind and exact serialized typed value.
- Numeric, nil, validity, and conversion-error states independently.
- Original `contextRef` and `unitRef` strings even if resolution fails.
- Decimals, precision, language, fact XML ID, and Inline attributes such as scale,
  sign, format, escape, and continuation references when present.
- Source document, XML node path, source order, tuple parent, and optional line.

Do not describe `model_fact.value` as the original HTML bytes. The retained source
file is the exact byte record; Arelle's lexical and typed values are separate
interpretations. Keep full text blocks without application truncation. Do not call
`str()` on arbitrary Arelle objects to serialize typed values: provide explicit
converters for numbers, text, booleans, dates, QNames, and XML. Unsupported types
retain their source representation and a conversion diagnostic.

Capture the exact legacy report label selected by Arelle while the model is alive,
as well as the current unit display string and ordered dimension-display strings.
Store these explicitly as compatibility data alongside the structured evidence.
They are not authoritative dimension values: the current typed-dimension formatter
uses an Arelle object's representation, which cannot be reconstructed reliably from
typed-member text alone. Replay uses the captured strings; semantic queries use
expanded names, measures, and namespace-aware XML. Test competing English labels
and typed dimensions against a legacy projection captured from the same model.

### 5.3 Contexts, units, and dimensions

Preserve referenced contexts and units, plus the model's context/unit inventory.
Use occurrence identity within the snapshot, not a uniqueness assumption about XML
IDs. Keep missing or ambiguous references as diagnostics instead of picking a row.

Contexts retain entity scheme/identifier, instant/duration/forever/unknown period,
original period strings, interpreted dates, and segment/scenario XML. Preserve
Arelle's exclusive end timestamp separately from the report-facing inclusive date.
Apply the current one-day adjustment only where the current date semantics apply;
do not erase original timestamps or timezone information.

Explicit dimensions retain expanded axis and member names. Typed dimensions retain
namespace-aware serialized XML, display text, and a canonical hash. Preserve whether
the content came from segment or scenario. Defaults from the taxonomy are not
silently inserted as though they were explicitly reported dimensions.

Units retain ordered numerator and denominator measures with expanded names.
Canonical hashes aid comparisons; they never merge distinct source occurrences.

### 5.4 Validation and calculation evidence

Capture raw JSON log output before closing the session. Store log-level and
truncation settings in the extraction profile and store parsed records in order.
Preserve raw reference objects; link them to facts only when the locator resolves
unambiguously. Filing-level or ambiguous messages must remain filing-level or
ambiguous. An invalid log payload is a recorded extraction failure, not an empty
message list.

The initial log profile preserves the existing warning-and-above scope. It does not
claim to contain every informational message. Retain the exact returned log file;
any Arelle-side truncation is part of the recorded profile, not hidden completeness.

Extract effective calculation relationships for the supported Calculation 1.0 and
1.1 summation-item arcroles present in the installed Arelle version. Preserve the
arcrole, link role, parent/child concept, exact weight/order, link/arc QNames, and
source locator. Mark a network `extracted_empty` only after a successful lookup;
record failure as `extraction_failed`, which prevents a complete storage snapshot.
This is an effective-network export, not a claim to retain every prohibited or
overridden raw arc. Required filing linkbase files retain their original bytes.

Do not infer numerical reconciliation success from the existence of a calculation
relationship. The report projection continues to expose its current Calculation
1.0 relationship set and ordering.

## 6. SQLite schema

The lists below are the required logical columns; implementation should turn them
into explicit DDL in `0001_evidence.sql`. IDs are integer primary keys unless stated
otherwise. Dates/timestamps are ISO-8601 text, timestamps include UTC, and exact
numeric fields are text. Required/optional rules below are part of the contract.

### 6.1 Identity, attempts, and snapshots

| Table | Required columns and responsibility |
|---|---|
| `schema_migrations` | `version` PK, filename, checksum, applied_at |
| `companies` | id, unique zero-padded CIK, current name; optional ticker; created_at, updated_at |
| `filings` | id, company_id, accession, form, filing_date, report_date, primary_document, source_url; unique accession |
| `processing_runs` | id, company_id, purpose, requested-window JSON, owner_pid, owner_process_start_identity, started_at, optional completed_at/error_summary, status |
| `processing_run_filings` | id, run_id, filing_id, status, started_at, optional completed_at/snapshot_id/error_code/error_text; unique run_id + filing_id |
| `evidence_snapshots` | id, filing_id, completed_by_attempt_id, captured_company_name, captured_filing_metadata_json, source_manifest_hash, extraction_profile_hash, extraction_profile_json, payload_hash, coverage_manifest_json, captured_at; optional captured_company_ticker, fiscal_year/fiscal_period with their source |

`processing_runs.status`: `running`, `succeeded`, `partial`, `failed`, `interrupted`.
Per-filing status: `pending`, `processing`, `stored`, `reused`, `failed`, `interrupted`.
Attempts and filing metadata may survive failure; partial snapshot evidence may not.

`evidence_snapshots` contains completed snapshots only. Unique key:
`(filing_id, source_manifest_hash, extraction_profile_hash)`. A repeated key with a
different payload hash is an integrity failure requiring investigation, not an
update. The snapshot's completing attempt and every subsequent reuse attempt link
to the same stored snapshot. Insert the snapshot and update its attempt in one
transaction; the attempt's snapshot link is nullable until that transaction commits.

Company CIK and filing accession ownership are stable identities. Snapshot loading
uses captured company name/ticker and captured filing metadata, including form,
dates, primary document and source URL. It does not substitute the current company's
display metadata. The small metadata JSON object is versioned and immutable, not a
second copy of the filing's fact set. Updating a current company name or ticker must
not change an old bundle or its report header.

The extraction profile includes application/extractor version, Arelle version,
validation options, SEC transform-plugin revision and hashes, and serialization
version. Exclude credentials, user-agent contact details, timestamps, and local
absolute paths from the profile fingerprint.

Define fingerprints before writing the repository's deduplication logic:

- `source_manifest_hash`: SHA-256 of versioned canonical JSON for original source
  resources and dependencies, sorted by original URI and resource kind, including
  content hashes and explicit unavailable-hash states. Exclude logs, staging paths,
  generated-document cache names, and capture timestamps. A missing optional
  dependency hash is recorded, never represented as a verified checksum.
- `payload_hash`: SHA-256 of versioned canonical JSON for the detached semantic
  evidence. Use source occurrence keys (original document identity plus node path/
  source order) instead of database IDs. Sort unordered collections by those keys;
  preserve meaningful sequence fields such as candidate, measure and message order.
  Include exact values, concept metadata/labels, context content, dimensions,
  relationships and meaningful validation content/references. Exclude report
  evaluations, which have their own rule version and can be added later.
- Exclude attempt IDs, database IDs, captured/evaluated timestamps, process identity,
  timings, runtime object IDs and current company discovery display metadata.
  Captured name/ticker remain preserved with the completing snapshot, but a later
  company rename alone does not make equivalent source evidence a hash mismatch.
  Canonicalize identified cache-path/reference fields
  to original document identities. Keep a documented allowlist of operational fields
  to exclude; do not strip arbitrary validation text or financial values. Captured
  legacy object-display strings are retained for replay but excluded from semantic
  hashing because object indexes may vary between equivalent runs.
- Serialization uses UTF-8, sorted object keys, explicit nulls, stable separators,
  and exact numeric strings. Its version belongs in the extraction profile. Store
  original records and exact log bytes unchanged; canonicalization operates on a
  separate representation used only for hashing.

Test equivalent extractions with different temporary directories, attempt IDs,
database-assigned IDs and log timestamps. Semantic hashes must match. Changing an
actual fact value, dimension, selected label, relationship, or substantive validation
message must change the payload hash. A mismatch when an input dependency had no
verifiable hash is a source-provenance ambiguity to investigate, not proof that the
stored database is corrupt.

For an existing accession, validate discovery's immutable filing identity and
report-period metadata against the stored filing record. Unexpected disagreement
raises a filing-metadata error for investigation; it must not silently reclassify
an existing snapshot under a different report date.

### 6.2 Files and source documents

| Table | Required columns and responsibility |
|---|---|
| `artifacts` | id, unique SHA-256, relative object path, byte_size, media_type, created_at; immutable bytes |
| `snapshot_artifacts` | snapshot_id, artifact_id, purpose, logical_name; unique snapshot_id + purpose + logical_name |
| `attempt_artifacts` | attempt_id, artifact_id, purpose, logical_name; unique attempt_id + purpose + logical_name |
| `source_documents` | id, snapshot_id, original_uri, document_kind, retention_kind; optional artifact_id, content_hash, parent/source-reference details |

Artifacts have no mandatory filing owner: one file can support several snapshots,
and later evidence packets can span several filings. Typed link tables establish
ownership. `retention_kind` distinguishes `retained_original`,
`external_dependency_reference`, and `generated_document`.

Required retained resources have an artifact link and hash. External dependency
rows retain original URLs, available hashes and version information, but do not
promise offline replay. Generated Arelle documents are identified as generated,
never mislabeled as original SEC files.

Keep exact raw logs per attempt, including failed attempts when capture was possible.
Install and register these audit artifacts with the attempt in a short transaction
separate from the snapshot transaction. A snapshot links the completing attempt's
log; a reused extraction may retain different operational logs without changing
the original snapshot. Failed-attempt logs are audit records, not partial snapshots.

### 6.3 Concepts, contexts, units, and facts

| Table | Required columns and responsibility |
|---|---|
| `concepts` | id, namespace_uri, local_name; unique namespace_uri + local_name |
| `snapshot_concepts` | snapshot_id, concept_id, display_qname, definition_status; optional data_type expanded name, period_type, balance, numeric/abstract flags, source_document_id, source_locator |
| `concept_labels` | id, snapshot_id, concept_id, role_uri, language, label_text, source_order; optional source document/locator |
| `contexts` | id, snapshot_id, source_order, source_document_id, source_locator, raw XML, period_kind; optional XML ID, entity scheme/identifier, raw period values, interpreted dates, canonical_hash, legacy_report_dimensions_json |
| `context_dimensions` | id, snapshot_id, context_id, source_order, axis expanded name, member_kind, context_element; optional axis/member concept IDs, explicit member expanded name, typed XML/text/hash |
| `units` | id, snapshot_id, source_order, source_document_id, source_locator, raw XML; optional XML ID, legacy_report_unit_text, canonical_hash |
| `unit_measures` | unit_id, side, measure_order, namespace_uri, local_name, display_qname; PK unit_id + side + measure_order |
| `facts` | id, snapshot_id, source_order, observation_origin, fact_kind, source_document_id, source_locator, is_nil, validity_code/name; optional concept_id/QName, context_id, unit_id, raw contextRef/unitRef, parent_fact_id, XML ID, source line, top_level_order, raw_value_text, typed_value_kind/text, is_numeric, decimals, precision, language, Inline metadata JSON, legacy_report_label_text |
| `extraction_diagnostics` | id, snapshot_id, code, severity, message, source_order; optional fact/context/unit/source-document links and raw details |

Every fact has a unique `(snapshot_id, source_order)`. There is deliberately no
unique key over concept/context/unit/value. A missing resolved concept is allowed;
the original QName and locator remain when available. `snapshot_concepts` holds
metadata and labels per extraction so importing another filing cannot change the
meaning or labels shown for old evidence. Include concepts used only in dimensions
or relationship endpoints, not just concepts with reported values.

Use `observation_origin = recognized | undefined` and
`fact_kind = item | tuple | unresolved`. For expanded names, use separate
`<name>_namespace_uri` and `<name>_local_name` columns. Context interpreted date
columns are `period_start_date` and `period_end_date`; retain raw date/timestamp
fields separately. The report-label capture is required for every report-eligible
fact. A context's legacy dimension list preserves ordering and exact strings.

Dimensions belong to contexts. Provide a read-only `fact_dimensions` view joining
`facts.context_id` to `context_dimensions.context_id`. This gives callers dimensions
linked to each fact without copying the same dimensional context onto every fact.

`member_kind` is `explicit`, `typed`, or `unresolved`. Enforce mutual exclusion of
explicit-member and typed-value columns for resolved dimensions. Preserve malformed
content in raw context XML plus a diagnostic. An unresolved value never acquires
fabricated type flags or a guessed member.

### 6.4 Report evaluation and conflicts

| Table | Required columns and responsibility |
|---|---|
| `report_evaluations` | id, snapshot_id, report_kind, rule_version, report_date, evaluated_at; unique snapshot_id + report_kind + rule_version |
| `fact_report_status` | evaluation_id, fact_id, evidence_role; optional selection_note, selected_fact_id; PK evaluation_id + fact_id |
| `fact_exclusion_reasons` | evaluation_id, fact_id, reason_code, detail; stable multiple-reason ordering |
| `reconciliation_issues` | id, evaluation_id, concept_id, reason_code, reason_text, issue_order |
| `reconciliation_issue_facts` | issue_id, fact_id, candidate_order; PK issue_id + fact_id, unique issue_id + candidate_order |

Evidence roles: `selected_primary`, `dimensional`, `duplicate_not_selected`,
`conflict_candidate`, `excluded`. Every stored observation has one role for each
completed evaluation; a fact can have several exclusion reasons. These are report
decisions, not global fact validity or future metric decisions.

Evaluation links must refer to the evaluation's snapshot. A new report rule creates
a new evaluation from stored evidence and preserves the old evaluation; it need
not rerun Arelle. E/D/R labels are allocated only when rendering a chosen evaluation.

Persist a later evaluation through its own short transaction. An existing evaluation
key is reused only when its decisions match; changed decisions under the same
rule version are an error. New rules must use a new version. This operation cannot
change the immutable snapshot, its original display captures or its payload hash.

### 6.5 Validation and relationships

| Table | Required columns and responsibility |
|---|---|
| `validation_messages` | id, snapshot_id, message_order, level, code, message_text, raw_record_json; unique snapshot_id + message_order |
| `validation_references` | id, message_id, reference_order, raw_reference_json, resolution_status; optional fact_id/source_document_id/source_locator |
| `roles` | id, snapshot_id, role_uri; optional definition and source locator; unique snapshot_id + role_uri |
| `relationship_networks` | id, snapshot_id, arcrole_uri, extraction_status, relationship_count; unique snapshot_id + arcrole_uri |
| `calculation_arcs` | id, snapshot_id, network_id, role_id, relationship_order, parent_concept_id, child_concept_id, exact_weight_text; optional exact_order_text, link/arc expanded names, source_document_id/source_locator, raw arc details |

Do not collapse two relationship occurrences solely because their four display
fields match. The report projection may perform the existing display deduplication.
If a required field cannot be extracted, preserve the source diagnostic and fail
completion rather than presenting a silently shortened relationship set as complete.

### 6.6 Constraints and indexes

Use foreign keys (database-enforced links) on every relationship. Enable them on
every connection. Add `(snapshot_id, id)` candidate keys to scoped parent tables
and composite foreign keys for fact/context/unit, tuple-parent, dimension, and
relationship links. For evaluation and message link tables, carry snapshot_id where
needed to enforce the same boundary. Do not rely only on Python checks.

Add `CHECK` constraints for enumerated states, 0/1 booleans, nonnegative byte sizes,
positive IDs, and mutually exclusive dimension fields. Optional numeric/type
classification flags must allow unknown. Source XML IDs are indexed, not unique.

Initial indexes should serve these queries:

| Index | Query served |
|---|---|
| `filings(company_id, form, report_date, accession)` | Filing history and selected windows |
| `evidence_snapshots(filing_id, extraction_profile_hash, id)` | Compatible stored extraction |
| `facts(snapshot_id, concept_id, source_order)` | All occurrences of an observed concept |
| `facts(snapshot_id, context_id)` and `facts(snapshot_id, unit_id)` | Context/unit evidence retrieval |
| `contexts(snapshot_id, period_kind, period_end_date, period_start_date)` | Exact-period filtering |
| `context_dimensions(snapshot_id, axis namespace/local, explicit member namespace/local)` | Dimensional evidence search |
| `fact_report_status(evaluation_id, evidence_role, fact_id)` | Selected, excluded and conflicting evidence |
| `reconciliation_issue_facts(issue_id, candidate_order)` | Ordered conflict candidates |
| `calculation_arcs(snapshot_id, role_id, parent_concept_id)` | Role-scoped calculation children |
| `validation_messages(snapshot_id, message_order)` | Stable message replay |

Do not sort or aggregate monetary amounts through SQLite text or floating-point
casts. Parse exact numeric text into `Decimal` where arithmetic is later authorized.

## 7. Classification without changing reporting behavior

Version the existing report rules as `report-v1`. Check numeric/nil/validity,
context/unit presence, supported period kind, report-date match, annual duration
300-400 days or quarterly duration 60-120 days, then dimensions and duplicate/conflict
selection. Store reason codes such as:

```text
NON_NUMERIC                 NIL_VALUE
ARELLE_INVALID              NUMERIC_CONVERSION_FAILED
NON_FINITE_NUMERIC          MISSING_CONTEXT
MISSING_UNIT                UNSUPPORTED_PERIOD_TYPE
PERIOD_END_MISMATCH         ANNUAL_DURATION_OUT_OF_RANGE
QUARTER_DURATION_OUT_OF_RANGE
UNRESOLVED_CONCEPT          LEGACY_REPORT_TOP_LEVEL_ONLY
EXACT_DUPLICATE_NOT_SELECTED
CONFLICTING_DIMENSION_FREE_FACT
```

Only apply a check when its prerequisites are available: a missing context is not
evidence of a mismatched period end. Nil and invalid facts remain distinct even
when both also lack a usable numeric value.

Preserve current top-level report coverage for equivalence. Newly retained nested
or unresolved observations are stored and classified as outside `report-v1` rather
than silently expanding its output. Reuse the current precision ranking, concept
grouping, tie-breaking, and conflict semantics. Do not fix its entity-scope or
grouping limitations inside this storage milestone.

Link a nonselected duplicate to the selected fact and every conflict issue to all
its candidate facts. Candidate summary text is reconstructed from those records.
Never deduplicate or quarantine facts by deleting them from `facts`.

## 8. Artifact retention, versions, and recovery

### 8.1 Files to retain now

Retain the exact primary Inline XBRL document, every loaded Inline document in the
selected document set, and loaded filing-owned schema/linkbase resources under the
selected accession. This includes a document supplying only shared contexts, units
or continuation text, even when it contains no fact. Also retain the raw Arelle log
and a source manifest. Unrelated exhibits,
images, and all files in the SEC accession directory are not automatically acquired.

Inventory external taxonomies and other loaded dependencies with original URL,
available content hash, and version information. Archive coverage is explicit in
the manifest; a retained primary file is not described as a complete offline archive.

Use an attempt-private Arelle cache/staging location for the initial implementation
so another ingestion cannot replace a cached file while it is being captured.
Before closing the session, resolve original URIs to the exact local source files
the loader used and copy required bytes into immutable staging. Do not redownload
the primary document afterward or serialize its parsed XML as a substitute.

The initial adapter proof must verify this capture path for the installed Arelle
version, including a document with a relative schema reference. If a required
source file cannot be identified, the attempt fails with a specific capture error.

### 8.2 Layout and verification

Recommended runtime root on Windows: `%LOCALAPPDATA%/SECInlineFinancials/data`.
Keep the live database outside the OneDrive-backed checkout. Constructors accept
explicit paths; tests use temporary directories. Source code and the plan remain
in the checkout. Database location is a configurable implementation default, not a
request to move any existing user files.

```text
data/
  evidence.sqlite3
  objects/sha256/<first-two-hash-characters>/<full-hash>
  staging/<attempt-id>/
```

Paths are relative to the configured artifact root. Verify hash and byte size
before installation; never overwrite an existing hash-named object unless its
bytes already match. Resolve download requests through artifact IDs. Reject
absolute paths, traversal, or resolved symlink/reparse-point escapes from the root.

Flush staged files before installing them on the same volume. The source manifest
uses deterministic serialization of original URIs, resource kinds, hashes and
retention states. Keep changing attempt timestamps outside its content fingerprint.

### 8.3 Commit protocol

SQLite transactions protect database rows; they do not roll back ordinary file
renames. The protocol deliberately permits recoverable orphan files, while keeping
incomplete snapshots invisible. See [SQLite atomic commit](https://sqlite.org/atomiccommit.html).

1. Record a processing attempt; perform SEC/Arelle work outside write transactions.
2. Detach and validate the complete bundle, classify it, and check coverage counts.
3. Stage, hash, flush and install required immutable objects; verify existing objects.
4. Begin a short `BEGIN IMMEDIATE` transaction and recheck the snapshot unique key.
5. If an identical complete snapshot exists, verify its payload hash, mark the
   attempt reused, and commit. Otherwise insert the snapshot and every child record.
6. Translate temporary observation references into permanent IDs; check links/counts.
7. Insert the initial report evaluation, link installed artifacts, mark the attempt
   stored, and commit. A reader sees either the previous database or all new rows.
8. Remove only the successful attempt's staging files after confirmed completion.

The caller owns no hidden nested commits. If commit returns an uncertain result,
open a new connection and look up the exact unique key before recording failure or
retrying. Failure records are written separately after rollback.

### 8.4 Interruption matrix

| Interruption point | Visible state | Recovery |
|---|---|---|
| Before source capture completes | Attempt metadata only | Mark interrupted after confirming owner is gone; retry |
| After staging but before object installation | Attempt plus staging files | Inspect only the abandoned attempt directory; retry or remove its temporary files |
| After object installation but before database commit | Complete unreferenced objects may exist | Retain and reuse matching objects on retry; do not pretend they are a snapshot |
| During the database transaction | SQLite rolls back the incomplete snapshot | Record failure/interruption; retry uses installed verified objects |
| After commit but before returning success | Complete snapshot and stored attempt exist | Lookup returns the existing snapshot without reinserting facts |
| Referenced file missing or hash mismatched later | Evidence rows exist, artifact health fails | Report integrity failure; preserve rows and diagnose, never silently recreate source evidence |

Startup recovery must not mark another live process's attempt interrupted. Record
process ownership with PID and start identity, or defer recovery when ownership
cannot be established. Do not automatically delete committed or unreferenced
immutable objects in this milestone. Garbage collection is a later policy.

## 9. SQLite lifecycle and repeat-ingestion rules

Open short-lived connections with foreign keys enabled, a bounded busy timeout
(initially 5 seconds), WAL mode, and `synchronous=FULL`. FULL is proposed because
this is durable evidence. SQLite documents that WAL with NORMAL can lose recently
committed transactions on power loss; FULL adds a sync at each commit. Filesystem
and hardware guarantees still apply. See [SQLite synchronous settings](https://sqlite.org/pragma.html#pragma_synchronous).

Run numbered, packaged SQL migrations in order. Apply each migration and its
checksum ledger entry in the same transaction. Refuse modified applied migrations
or a database newer than the application understands. Serialize competing migration
attempts with a write transaction and recheck the version after obtaining the lock.
Ensure the runner does not accidentally commit early through `executescript`.

Use one Arelle session at a time per process; do not share a session across threads.
This milestone does not add parallel acquisition. Snapshot unique constraints and
the recheck under the write lock still prevent duplicate commits from competing
processes. Retried lock acquisition is bounded and does not repeat completed SEC
requests unnecessarily.

Repeat ingestion means no duplicate evidence, not a fresh verification of SEC state:

- A normal explicit ingest may reuse the most recently completed snapshot matching
  accession and extraction profile after verifying its required local artifacts.
- If that snapshot lacks the requested report-rule evaluation, load its stored
  bundle, classify it, and save the evaluation separately. Reusing source evidence
  does not require an Arelle run just to apply a newer report rule.
- A newer profile can create a new snapshot; earlier snapshots remain retrievable.
- Reusing local evidence does not claim that remote filing bytes were rechecked.
- If acquisition occurs and observes a different manifest, store a distinct source
  snapshot; do not overwrite the prior one. Choosing which snapshot feeds published
  metrics belongs to the later update/mapping design.
- Each query or projection uses an explicit snapshot ID. Never join facts across
  snapshots merely because their accession, concept, or XML IDs match.
- No destructive reimport, history pruning, or amendment-precedence policy is added.

For a company window, commit each filing independently. A failed filing leaves
earlier complete snapshots usable. Return per-filing outcomes and mark the run
partial when it contains both success/reuse and failure. The five-annual and
twelve-quarter window controls discovery, not deletion of older stored evidence.

Backup acceptance uses SQLite's backup API under quiescent ingestion plus all
referenced artifacts and a hash manifest. Do not copy only an open main database
file while ignoring its WAL. Verify restoration into a temporary runtime root.

## 10. Python interfaces and retrieval behavior

The implementation exposes these interfaces:

```text
ArelleProcessor.extract_evidence(company, filing, capture_area)
    -> FilingEvidenceBundle

classify_report(bundle, report_kind, rule_version)
    -> ReportEvaluation

EvidenceStore.initialize() -> schema version
EvidenceStore.find_reusable_snapshot(accession, extraction_profile_hash)
    -> SnapshotRef | None
EvidenceStore.save_snapshot(bundle, evaluation, attempt_id, installed_artifacts)
    -> StoreResult(snapshot_id, stored | reused)
EvidenceStore.load_snapshot(snapshot_id) -> FilingEvidenceBundle
EvidenceStore.get_report_evaluation(snapshot_id, report_kind, rule_version)
    -> ReportEvaluationRef | None
EvidenceStore.save_report_evaluation(snapshot_id, evaluation)
    -> ReportEvaluationRef
EvidenceStore.list_concepts(snapshot_id, cursor, limit) -> concept page
EvidenceStore.list_facts(snapshot_id, filters, cursor, limit) -> fact page
EvidenceStore.get_fact(fact_id) -> fact + source/context/unit/dimensions
EvidenceStore.get_conflict(issue_id) -> issue + ordered candidates
EvidenceStore.list_calculation_children(snapshot_id, role_uri, parent_concept)
EvidenceStore.list_validation_messages(snapshot_id, cursor, limit)
EvidenceStore.resolve_artifact(artifact_id) -> verified path + metadata
EvidenceStore.audit_snapshot(snapshot_id) -> counts, links, artifact checks

EvidenceIngestionService.ingest_filing(company, filing) -> FilingOutcome
EvidenceIngestionService.ingest_company_window(ticker, annual_count=5, quarterly_count=12)
    -> RunOutcome

project_report(snapshot_id, evaluation_id)
    -> AnnualResult | QuarterlyResult
```

The store controls SQL and connection ownership. Query results are detached and
ordered explicitly. Start fact pagination at 100 rows with a maximum of 1,000;
cursor by permanent fact ID within an explicit snapshot. Exact-period and concept
filters are available, but report eligibility is opt-in so later discovery can see
the full concept catalog and excluded evidence.

Loading and querying must not instantiate Arelle, contact SEC, reread the disposable
Arelle cache, or require a TXT report. Full snapshot loading exists for round-trip
verification; ordinary callers use bounded queries. Artifacts are verified at import,
reuse, explicit audit, and file retrieval.

Errors distinguish schema incompatibility, busy database, capture failure,
extraction failure, snapshot mismatch, missing artifact, and hash mismatch. Do not
return an empty result for an operational failure.

## 11. Later storage extensions, without premature mapping policy

Use new migrations when the corresponding milestone defines its behavior. The
evidence schema must allow these links without rewriting old facts:

| Later entity | Required evidence relationship |
|---|---|
| `target_metrics` | Stable metric key and definition version |
| `metric_evaluations` / `metric_results` | Company, exact period, metric definition, policy version, outcome, exact value/unit when present; explicit snapshot lineage |
| `metric_result_facts` | Result-to-fact links with roles; support several source facts without embedding comma-separated IDs |
| `evidence_packets` | Exact retained packet artifact, serialization/version metadata, target metric/period, all snapshot and fact links |
| `model_requests` / `model_responses` | Exact submitted/returned payload artifacts, model/configuration, timestamps and packet identity; authentication secrets excluded |
| `mapping_recommendations` | Proposed concept/rule and supporting packet/response/facts; pending until reviewed |
| `review_decisions` | Append-only approval/rejection history, reviewed recommendation version, timestamp and reason |

Generic artifacts already support these files, but no packet is generated and no
recommendation is requested in Milestone 1. Mapping exclusion reasons belong to
metric evaluations, not the report-exclusion table. A review decision or new mapping
must never mutate the facts that originally supported it.

The initial store is not a complete mapping-candidate engine: presentation networks,
metric precedence, and target-specific evidence selection need their own design.
Observed-concept enumeration prevents a weak report shortlist from being mistaken
for an exhaustive filing search.

## 12. Implementation work packages

Complete each package with its acceptance evidence before depending on it. The
critical path is P0 -> P1/P2 -> P3 -> P4/P5 -> P6 -> P7 -> P8. P1 and P2 can be
developed independently after the extraction proof; P4 and P5 meet at the bundle
and artifact contracts. Parallel development is optional, not a runtime requirement.

### P0. Prove capture and freeze the current report contract

Files: adapter tests, report fixtures, a small local Inline XBRL fixture with
relative schema/linkbase references.

- Verify real Arelle traversal covers top-level, nested, nil, nonnumeric and invalid
  observations, with a separate unresolved-element fixture if the parser rejects
  combining these in one valid document.
- Verify exact original-source capture and log extraction before Session closure.
- Include a multi-document Inline fixture whose continuation/context provider has
  no facts, and verify that all participating source documents are retained.
- Record deterministic annual and quarterly golden reports, including dimensions,
  duplicate selection, conflicts, validation messages, and calculation relationships.
- Establish a source-order policy and repeat the extraction to verify stable hashes.

Exit: observed identities/counts are understood; archived hashes match parser input;
the unchanged renderer matches its goldens. Resolve adapter assumptions here before
freezing DDL. This is the first concrete implementation assignment.

### P1. Add detached evidence types and complete extraction

Files: `evidence_models.py`, `evidence_extraction.py`, focused adapter additions/tests.

- Define nullable fields, explicit typed-value converters, locators, provenance,
  context/unit/dimension records, and extraction diagnostics.
- Extract before report filtering and detach every required field while the model
  is alive. Capture fiscal metadata without discarding its original facts.
- Count observed objects independently and compare to emitted occurrence records.
- Preserve all exposed duplicate candidates and raw references.

Exit: the bundle remains usable after session closure; serialization preserves
every fixture observation and exact value without accessing Arelle properties.

### P2. Add migrations, configuration, and relational constraints

Files: `storage/database.py`, `storage/migrations.py`, `storage/sql/0001_evidence.sql`,
storage configuration and packaging declarations.

- Implement all initial tables/views and the scoped foreign keys.
- Configure connections and transactional, checksum-verified migrations.
- Package SQL resources so installation works outside the source checkout.
- Use injectable runtime paths; keep live data out of tracked source files.

Exit: clean/repeated initialization works; failed migrations roll back; altered
migrations/newer schemas fail clearly; cross-snapshot links are rejected by SQLite.

### P3. Classify reports without losing observations

Files: `evidence_classification.py`, focused reconciliation adapter and tests.

- Implement versioned evaluation and prerequisite-aware exclusion reasons.
- Reuse the current report semantics; retain selected and nonselected occurrences.
- Generate permanent-link-ready candidate references and duplicate-selection links.

Exit: roles cover the entire bundle; report selections/conflicts equal the legacy
results; a nil, YTD, invalid, or comparative fact remains in the observation set.

### P4. Install immutable artifacts

Files: `storage/artifacts.py`, capture adapter, artifact tests.

- Implement attempt staging, exact-byte hashing, relative-path resolution,
  no-overwrite installation, required-file manifest, and integrity checks.
- Distinguish original, generated, and external-reference resources.
- Handle an already present object, missing source, corrupt object, and interruption.

Exit: a required file cannot be linked as valid unless its bytes and path checks
pass; equivalent content can be shared; no committed object is deleted by cleanup.

### P5. Save and reload one complete snapshot

Files: `storage/evidence_store.py`, transaction/round-trip tests.

- Persist metadata, observations, evaluation, relationships and artifact links in
  one transaction; translate temporary references into permanent IDs.
- Implement snapshot fingerprints, reuse recheck, and immutable version handling.
- Implement full bundle reconstruction and an audit report with counts/hash results.

Exit: one accession round-trips after closing the connection and Arelle session;
injected failures at multiple insert stages leave no partial snapshot; repeated
saves return the same snapshot and fact IDs. Changing current company metadata
cannot change a reloaded historical bundle. Fingerprints remain stable across
different attempt directories and operational log timestamps.

### P6. Add retrieval and explicit report projection

Files: repository query functions, `storage/report_projection.py`, retrieval tests.

- Add bounded fact/concept queries, exact-period filters, dimensions, conflicts,
  role-scoped calculations, messages, and artifact resolution.
- Add separate evaluation lookup/save so a new rule can classify stored evidence
  without altering a snapshot or invoking Arelle.
- Rebuild existing report read models from a chosen snapshot/evaluation.
- Keep current report command routing and output filenames unchanged.

Exit: with SEC/Arelle calls disabled and the extraction cache unavailable, stored
evidence can be queried and the fixture report output is byte-identical.

### P7. Add explicit ingestion and interruption recovery

Files: `evidence_ingestion.py`, `storage/recovery.py`, orchestration tests.

- Expose Python entry points for one filing and the existing 5/12 discovery window.
- Reuse compatible complete snapshots; record per-filing and aggregate outcomes.
- Separate file installation from the database commit as specified above.
- Add restart reconciliation with live-owner checks and no automatic object pruning.
- Keep frontend-triggered refresh and scheduling out of this implementation.

Exit: a mixed multi-filing run retains completed filings after another fails; retry
does not repeat stored work; ingestion creates no inspection report.

### P8. Verify real evidence, packaging, and restoration

Files: opt-in live acceptance test, storage runbook, acceptance evidence artifact.

- Process one real 10-K and one real 10-Q; reconcile extracted/stored/reloaded
  identities and counts, not counts alone.
- Restart and load without Arelle or network. Verify excluded facts, exact values,
  context/dimension links, candidate links, messages, relationships, and hashes.
- Exercise the complete selected 5/12 filing window in an opt-in acceptance run.
- Perform a quiescent backup/restore into a separate temporary root and audit it.
- Build/install the package and verify initialization can locate its SQL resources.
- Record elapsed time, database size, artifact size, largest text fact, and peak
  memory for these fixtures. Set performance budgets from measurements.

Exit: record the exact accessions, profiles, counts, hashes, commands and outcomes.
A one-accession pass proves the storage round trip; only the separate window run
proves full-window orchestration. No claim of SEC EFM-complete validation is added.

## 13. Acceptance matrix

| Case | Required result |
|---|---|
| Numeric zero, nil, empty string, invalid text and absent concept | Remain distinguishable after reload |
| Large integer, negative, scaled Inline value, exact decimal, `INF` precision | Exact source/typed representations survive |
| Nonnumeric DEI and full text block | Stored despite being outside numeric reports |
| YTD, comparative, off-report-date and forever contexts | Retained with correct dates and exclusion reasons |
| Missing concept/context/unit and malformed typed value | Raw references and diagnostics retained; no fabricated replacement |
| Nested tuple and duplicate occurrences | Every exposed occurrence retained once with parent/source links |
| Two documents reuse XML context/fact IDs | No collision or accidental cross-document join |
| Same concept receives different filing labels | Each snapshot replays its own labels and metadata |
| Competing effective English labels and typed dimensions | Captured legacy strings reproduce the original in-session report projection |
| Company changes its name/ticker after extraction | Old snapshot metadata and report header remain unchanged |
| Continuation/context provider has no facts | Its original Inline document is still retained |
| Equivalent extraction in a different attempt/cache directory | Semantic fingerprint remains stable; exact per-attempt logs remain available |
| New report-rule version applied after extraction | Adds an evaluation without an Arelle run or mutation of the original snapshot |
| Explicit/typed dimensions; segment/scenario content | Structured links plus namespace-aware content survive |
| Duplicate selection and conflicting candidates | Same report result; every candidate has a permanent fact link |
| Same parent in different roles/arcroles | Networks remain separate; weights exact |
| No calculation network versus extraction exception | Successful empty and failed extraction stay distinct |
| Ambiguous validation reference | Raw reference remains; no guessed fact link |
| Failed import or migration | No partial committed snapshot or migration |
| Repeat ingest and competing commit | Same complete snapshot reused; no duplicated occurrence rows |
| Interrupted file/database transition | Recovery matches the interruption matrix |
| Report compatibility and ingestion behavior | Golden reports unchanged; ingestion writes no TXT reports |
| Restart with network/Arelle/cache unavailable | SQLite retrieval and retained-file access still work |
| Missing/corrupted artifact | Explicit integrity failure, preserved historical rows |
| Restored backup and installed package | Restored evidence audits; packaged migrations initialize |

Run the existing project checks after implementation:

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy src
```

Live acceptance remains opt-in using the existing SEC configuration mechanism.
Do not place credentials or user-agent contact information in acceptance artifacts.
The local unit, round-trip, rollback, recovery, backup, lint, format, and type checks
passed on 2026-09-08. The real-filing and full-window acceptance rows remain pending.

## 14. Review status and remaining implementation probes

Independent design review: PASS, 9/10, after two passes. Four findings were fixed:
retention of Inline documents with no facts, exact legacy display capture, canonical
snapshot fingerprints, and immutable captured company/filing metadata. No material
review concerns remain. This is document review, not implementation verification.

The owner selected a fresh proposal-based plan and stored-evidence replay. The
implementation now lives in `evidence_models.py`, `evidence_extraction.py`,
`evidence_classification.py`, `evidence_ingestion.py`, and the `storage` package.
The legacy report command does not depend on these modules and remains unchanged.

Local fixtures verify exact source capture and undefined/nested observation
detachment, but the installed-Arelle multi-document fixture and real-filing probes
remain acceptance work. This is not permission to reduce the all-observed-facts
requirement. A missing required resource or unrepresentable exposed observation
still fails the import instead of committing a shortened snapshot.

Milestone 1 is complete when the acceptance matrix and the scoped acceptance runs
pass, a documented backup can restore the evidence, and the existing report workflow
still behaves as specified. Later mapping, recommendation and update milestones
retain their own acceptance gates.
