# Software Design Description

## 1. Document status

| Field | Value |
| --- | --- |
| System | SEC Inline Financials |
| Package | `sec-inline-financials` 0.1.0 |
| Baseline | `matt` working tree on 2026-10-13 |
| Runtime | Python 3.10+, Arelle 2.41.x, SQLite |
| Verification | 134 offline tests passed on 2026-10-13 |

This document describes the implemented local SEC/XBRL evidence pipeline in the
current working tree. The stored-company scanner and post-purge SQLite compaction
are present as uncommitted working-tree changes at this baseline. Planned LLM,
HTTP API, and browser-frontend layers are identified explicitly and are not
described as implemented.

## 2. Purpose and scope

SEC Inline Financials acquires selected SEC Inline XBRL filings, processes them
with Arelle, and retains enough source and structured evidence to reproduce and
audit later financial-metric decisions. Its current user interface is a set of
local command-line programs and inspection scripts.

The implemented scope includes:

- exact-form Inline XBRL `10-K` and `10-Q` discovery;
- operator-verified CIK lineage across reporting-entity changes;
- Arelle extraction, validation, source capture, and narrative-section parsing;
- immutable filing evidence and content-addressed artifact storage;
- versioned `report-v2` fact classification;
- versioned `direct-mapping-v2` evaluation of seven financial metrics;
- due-date-based local company scanning;
- preview-first lineage maintenance and company purge;
- read-only report, filing-section, and mapping inspection tools; and
- backup, recovery, artifact verification, and database integrity checks.

The following remain planned: LLM mapping recommendations, human mapping-review
storage, a stable local HTTP API, a production browser frontend, scheduled jobs,
and automatic history pruning.

## 3. Design goals

1. **Evidence before presentation.** Preserve the filing resources, each observed
   fact occurrence, its context and unit, and its source lineage before building
   reports or metric values.
2. **Deterministic decisions.** Version extraction, classification, and mapping
   policies. Reuse prior results only when their identities and hashes match.
3. **Explicit missing states.** Never guess a value, infer a fourth quarter, or
   replace a reported zero with a fallback concept.
4. **Safe local operation.** Keep SQLite and artifacts behind Python services,
   require explicit migration approval, and make destructive operations preview
   first.
5. **Historical auditability.** Publish mutable pointers to active snapshots and
   metric evaluations without rewriting their immutable history.

## 4. Users and external systems

| Actor or system | Responsibility |
| --- | --- |
| Local operator | Runs CLIs, approves migrations and lineage changes, requests purge, and inspects results. |
| SEC EDGAR | Supplies ticker identity, submissions metadata, filing documents, and filing-owned taxonomy resources. |
| Arelle | Loads and validates Inline XBRL and exposes facts, contexts, units, relationships, and diagnostics. |
| Local filesystem | Stores the SQLite database, staging directories, Arelle cache, immutable objects, and optional reports. |
| Planned frontend/LLM | Future consumers of stable backend evidence; no production integration exists yet. |

## 5. Functional requirements

### FR-1: Ingest or refresh companies

The system shall accept one or more space- or comma-separated tickers. A batch
runs sequentially, reuses one lazy SEC client, continues after a per-company
error, and returns a nonzero exit code when any company fails.

For each ticker, the service shall:

1. validate the ticker and requested annual/quarterly counts;
2. inspect local refresh state before constructing an SEC client;
3. skip network and Arelle work when the stored window is current and complete;
4. otherwise discover up to five annual and twelve filed-quarter filings by
   default;
5. process or reuse each filing independently;
6. publish only successfully bound snapshots into the active window; and
7. run Direct Mapping only after at least one newly discovered accession was
   processed successfully.

### FR-2: Scan stored companies

The scanner shall list every stored ticker in deterministic order, compare its
annual and quarterly next-check dates with one captured scan date, skip current
companies, and refresh due or forced companies sequentially. It shall continue
after a company error and treat a failed refresh that falls back to local data as
a scan failure.

### FR-3: Discover filings across verified CIK lineage

The SEC client shall use only exact-form, Inline XBRL `10-K` and `10-Q` rows.
Annual periods are keyed by report year; quarterly periods are keyed by report
date. Current-CIK filings fill slots before predecessor-CIK filings, and older
lineage members may fill gaps but may not overwrite a current member's period.

Lineage is operator supplied. The system shall not infer it from names, tickers,
former names, accession prefixes, or transaction descriptions.

### FR-4: Extract complete detached evidence

Each non-reused filing shall run in an Arelle `Session` configured for online
loading, validation, calculation checks, duplicate-fact messages, a pinned SEC
transformation plugin, and the narrowly scoped SEC taxonomy retry plugin.

Before the session closes, the extractor shall detach:

- filing and company identity;
- retained and referenced source-document metadata;
- concepts and labels;
- contexts, dimensions, and units;
- every recognized or undefined fact occurrence;
- validation messages and resolved/unresolved references;
- calculation networks and arcs;
- coverage counts and raw Arelle logs; and
- required 10-K or 10-Q narrative-section records.

### FR-5: Classify report evidence

`report-v2` shall assign every observation exactly one evidence role:
`selected_primary`, `dimensional`, `duplicate_not_selected`,
`conflict_candidate`, or `excluded`.

Eligible facts must be top-level, numeric, non-nil, finite, Arelle-valid, have a
resolved instant/duration context and unit, end on the filing report date, and use
a supported duration. Annual durations are 300-400 days; quarterly durations are
60-120 days.

For a verified pre-transition report, `report-v2` may remove exactly one matching
DEI `LegalEntityAxis` wrapper or accept a direct predecessor-entity context. It
must preserve other dimensions and reject unrelated entity contexts.

### FR-6: Persist evidence and artifacts

The system shall store structured evidence in one normalized SQLite database and
source bytes in a SHA-256-addressed object store. One filing attempt either stores
its complete snapshot and report evaluation in one database transaction or stores
no partial snapshot.

Snapshot identity combines filing identity, source-manifest hash, extraction
profile hash, and semantic payload hash. Reuse requires artifact path, byte-size,
and SHA-256 verification.

### FR-7: Evaluate Direct Mapping

Direct Mapping shall evaluate these seven `target-metrics-v1` metrics independently
for every active annual or quarterly snapshot:

1. Revenue
2. Operating Income
3. Net Income
4. Total Assets
5. Total Liabilities
6. Equity
7. Operating Cash Flow

`direct-mapping-v2` shall select the first compatible configured US-GAAP concept
for an exact period. A result is either `reported`, linked to one selected fact,
or `missing`, with `mapping_not_found` or
`no_selectable_fact_for_period`. Publication shall recheck the active-window hash
inside the write transaction.

### FR-8: Maintain company lineage

The lineage CLIs shall resolve both supplied CIK identities through SEC metadata,
render the before/after direct edge, require an explicit `y`, and revalidate a
state hash in the write transaction.

Adding an older predecessor extends the oldest end of the lineage. Adding a new
successor transforms `B -> A` into `B -> A -> N`, preserves the company row and
ticker, and changes the canonical current CIK to `N`. Filing registrant and archive
owner identities remain immutable.

### FR-9: Purge company data

Company purge shall normalize and deduplicate requested tickers, include every CIK
and filing in each selected lineage, and preview row, artifact, staging-directory,
and byte counts before deletion. Execution shall remove exclusive rows and files,
preserve shared artifacts, retain failed file removals for retry, validate foreign
keys, and compact SQLite so freed pages return to the filesystem.

### FR-10: Inspect and recover evidence

Read APIs shall expose bounded pages of concepts, facts, validation messages,
calculation children, sections, conflicts, artifacts, and metric results. Recovery
shall mark attempts abandoned only when the owning process is no longer alive.
Backup shall require quiescent ingestion and include a hashed SQLite copy plus all
referenced immutable artifacts.

## 6. Non-functional requirements

| ID | Requirement | Design response |
| --- | --- | --- |
| NFR-1 Integrity | Corruption or identity drift must fail closed. | SHA-256 manifests, semantic hashes, foreign keys, migration checksums, `foreign_key_check`, `integrity_check`, and artifact verification. |
| NFR-2 Auditability | A published value must lead to its exact fact and filing. | Metric-result fact links, report-evaluation IDs, snapshot bindings, accession provenance, and immutable prior evaluations. |
| NFR-3 Determinism | Repeated identical input must reuse identical output. | Canonical JSON, ordered rules, stable hashes, idempotent lineage commands, and immutable rule-version identities. |
| NFR-4 Failure isolation | One bad filing or company must not destroy good evidence. | Per-filing attempts, per-snapshot transactions, sequential batch continuation, fallback to published local data, and mapping-after-publication separation. |
| NFR-5 Local security | Credentials and database access stay local. | Only `SEC_USER_AGENT` is loaded; frontend/API are absent; artifacts resolve beneath the configured root; migration and mutation actions need approval. |
| NFR-6 Concurrency | SQLite writes and Arelle global state must be serialized safely. | `BEGIN IMMEDIATE`, busy timeout, short write transactions, sequential company/filing processing, and no threaded Arelle sessions. |
| NFR-7 Storage efficiency | Repeated bytes should not multiply unnecessarily. | Content-addressed artifacts, string-column elision, and zlib compression/deduplication for compressible nonnumeric strings of at least 1 KiB. |
| NFR-8 Portability | The local application must run without a server deployment. | Python package, stdlib SQLite/HTTP clients, filesystem artifacts, and PowerShell-friendly CLIs. |

## 7. Architecture

The implemented architecture is a layered local application:

1. **Entry points:** CLI wrappers validate arguments and obtain explicit approval.
2. **Application services:** ingestion, scanning, lineage, mapping, and purge
   coordinate use cases.
3. **External adapters:** `SecClient` performs discovery and `ArelleProcessor`
   performs filing processing.
4. **Domain functions:** extraction, narrative parsing, `report-v2`
   classification, Direct Mapping, lineage validation, and fingerprints contain
   deterministic policy.
5. **Persistence:** `EvidenceStore` is the deep storage boundary over SQLite and
   `ArtifactStore`.
6. **Read projections:** report projection and inspection scripts rebuild
   human-readable outputs only from stored evidence.

See [Architecture](architecture.md) for the rendered system view and [UML](uml.md)
for static and runtime relationships.

## 8. Component design

| Component | Main responsibility | Important collaborators |
| --- | --- | --- |
| `CompanyIngestionService` | Local freshness decision, SEC discovery, publication, and conditional mapping. | `SecClient`, `EvidenceIngestionService`, `EvidenceStore`, `DirectMappingService` |
| `EvidenceIngestionService` | Per-run and per-filing orchestration, reuse, extraction, artifact install, and failure recording. | `ArelleProcessor`, `EvidenceStore` |
| `SecClient` | Ticker/CIK resolution and exact-form filing-window selection. | SEC ticker and submissions JSON |
| `ArelleProcessor` | One filing per Arelle session and complete detachment before close. | Arelle, SEC transform/retry plugins, extraction functions |
| `EvidenceStore` | Transactional persistence, publication pointers, lineage, bounded reads, and audits. | `EvidenceDatabase`, `ArtifactStore` |
| `DirectMappingService` | Load exact active inputs, reuse or compute mapping, and publish atomically. | mapping rules/resolver, `EvidenceStore` |
| `LineageMaintenanceService` | Build verified before/after lineage plans and apply approved changes before re-ingestion. | `SecClient`, `EvidenceStore`, ingestion callback |
| `CompanyDataPurger` | Preview and delete complete company-lineage scope while preserving shared objects. | `EvidenceStore`, SQLite temp scope tables, `ArtifactStore` |

## 9. Data design

### 9.1 Durable identity chain

```text
Company -> company CIK members -> predecessor/successor edges
        -> filings -> immutable provenance
        -> evidence snapshots -> report evaluations
        -> metric evaluations -> metric results -> selected facts
```

`companies` holds the stable local company identity and current ticker/CIK.
`company_ciks` and `company_cik_transitions` hold the verified unbranched lineage.
`filing_provenance` preserves the exact registrant and archive-owner CIK for every
filing even after the canonical company CIK changes.

### 9.2 Evidence snapshot aggregate

An `evidence_snapshot` owns or links:

- source documents and artifact links;
- narrative filing sections;
- concepts and labels;
- contexts and dimensions;
- units and measures;
- facts and extraction diagnostics;
- validation messages and references;
- roles, relationship networks, and calculation arcs; and
- versioned report evaluations, fact roles, exclusion reasons, and conflicts.

Cross-snapshot fact/context/unit links are forbidden by composite foreign keys.
Exact decimals remain text rather than SQLite `REAL`.

### 9.3 Publication model

`active_filing_snapshots` selects the current evidence window without deleting
older snapshots. `published_metric_evaluations` selects the current annual and
quarterly metric grids without modifying prior evaluations. Readers mark a metric
evaluation stale when its stored active-window hash no longer matches current
snapshot bindings.

### 9.4 Artifact model

Objects are stored under `objects/sha256/<prefix>/<digest>`. Artifact rows record
the digest, size, media type, and relative path. Snapshot and attempt link tables
record purpose and logical name. A shared digest may serve multiple companies and
is removed only after its final reference disappears.

## 10. Main runtime workflows

### 10.1 Ingest or refresh

```text
CLI -> schema preflight -> local company state
    -> [current] return reused_local
    -> [refresh needed] SEC discovery -> per-filing reuse/Arelle extraction
    -> publish stronger successful window
    -> [successful new accession] Direct Mapping
    -> result and per-filing status
```

### 10.2 Direct Mapping

```text
active filings + exact snapshots + report-v2 evaluations
    -> one bounded candidate-fact query
    -> ordered compatibility checks for seven metrics
    -> immutable metric evaluation and result/fact links
    -> published annual or quarterly pointer
```

### 10.3 Lineage change

```text
resolve both CIKs -> build before/after plan -> display edge -> explicit y
    -> recheck state hash -> commit lineage -> ingest canonical CIK
```

The lineage commit intentionally survives a later ingestion failure so an exact
rerun can retry without recreating the verified relationship.

### 10.4 Purge

```text
preview full company-lineage scope -> explicit execute flag
    -> transactional SQL deletion + pending file queue + integrity check
    -> delete exclusive files -> keep failures queued -> VACUUM
```

## 11. Public interfaces

| Command | Purpose |
| --- | --- |
| `sec-inline-financials TICKER...` | Primary ingestion/refresh command with elapsed time and resolved storage paths. |
| `sec-inline-financials-ingest TICKER...` | Lower-level ingestion/refresh command. |
| `sec-inline-financials-scan` | Scan all stored companies and refresh only due or forced companies. |
| `sec-inline-financials-map TICKER` | Evaluate a complete stored 5/12 Direct Mapping window. |
| `sec-inline-financials-lineage SUCCESSOR_CIK PREDECESSOR_CIK` | Add or update an older predecessor edge. |
| `sec-inline-financials-lineage-change PREDECESSOR_CIK SUCCESSOR_CIK` | Promote a new canonical successor CIK. |
| `sec-inline-financials-purge TICKER...` | Preview purge; add `--execute` to delete and compact. |

The inspection scripts under `tests/` are operator tools rather than installed
production commands. They read stored snapshots and do not contact SEC or open
Arelle.

## 12. Error handling and transaction boundaries

- SEC discovery errors fail an initial ingestion. When prior evidence exists,
  ingestion returns `refresh_failed_using_local_data` and keeps it published.
- Each filing produces `stored`, `reused`, or `failed`. A run becomes
  `succeeded`, `partial`, or `failed` from those outcomes.
- Mapping failure does not roll back a committed evidence window. The prior
  published metric evaluation remains readable and may be marked stale.
- Existing databases with pending migrations are unchanged unless the operator
  enters exactly `y` or a programmatic caller passes `allow_schema_upgrade=True`.
- Migration checksum drift, foreign-key violations, and integrity failures abort
  and roll back the migration transaction.
- Artifact reads verify location, size, and hash. Missing or changed artifacts
  block snapshot reuse and mapping input.
- An uncertain snapshot return is resolved by reconnecting and matching attempt,
  filing, source, profile, and semantic hashes.

## 13. Security and trust boundaries

1. SEC access requires an identifying User-Agent containing an identity and email.
2. Configuration loading reads only `SEC_USER_AGENT` from the process,
   `config.env`, or `config.txt`; it does not import arbitrary variables or keys.
3. SEC transform-plugin files are pinned to a commit and verified against packaged
   SHA-256 values before use.
4. The retry plugin retries only SEC Archives filing-extension `.xsd` URLs on HTTP
   503 with 1/2/4-second delays.
5. Filing HTML parsing disables network access. Extracted text is evidence, not an
   instruction source.
6. Artifact resolution rejects paths outside the configured object root and
   verifies content before returning it.
7. No authentication or remote API boundary exists because the implemented system
   is a local CLI application. A future HTTP service must define authentication,
   origin, and artifact-download controls before exposing this data remotely.

## 14. Deployment and operation

The default evidence root is resolved by `evidence_runtime_paths`; it contains:

```text
root/
  evidence.sqlite3
  objects/sha256/...
  staging/<attempt-id>/...
  cache/arelle/...
```

Install dependencies with `uv sync --link-mode copy`. Production commands use the
configured local evidence root. Arelle work and multi-company batches remain
sequential because Arelle has process-global state and SQLite has one local writer.

## 15. Verification strategy

The offline suite covers CLI batching, scanner due-date behavior, SEC metadata
shape validation, short filing windows, Arelle retry scope, `report-v2` transition
classification, lineage preview/apply/idempotency, migration approval and rollback,
mapping compatibility, snapshot/report identity, text-payload compaction, complete
lineage purge, shared-artifact retention, and physical database shrinkage.

Current verification:

```text
uv run --no-sync pytest -q
134 passed in 97.24s
```

This is offline evidence. It does not prove current SEC availability, a new live
accession path, or production behavior for every filer.

## 16. Implemented and planned boundaries

| Capability | State |
| --- | --- |
| SEC discovery, Arelle extraction, SQLite evidence, artifacts | Implemented |
| Narrative section extraction and stored inspection | Implemented |
| `report-v2` and seven-metric `direct-mapping-v2` | Implemented |
| Explicit scan, lineage maintenance, purge, recovery, backup | Implemented in current working tree |
| Automatic scheduled scanner | Not implemented |
| LLM recommendation packets/responses/review | Planned |
| Stable HTTP API and production frontend | Planned |
| Automatic retention pruning | Not implemented |

## 17. Source traceability

| Design area | Primary source |
| --- | --- |
| CLI and refresh policy | `company_ingestion.py`, `cli.py`, `schema_upgrade.py` |
| SEC discovery | `sec_client.py` |
| Arelle processing and detachment | `arelle_adapter.py`, `evidence_extraction.py`, `filing_sections.py` |
| Report classification | `evidence_classification.py` |
| Storage and publication | `storage/evidence_store.py`, `storage/database.py`, `storage/sql/*.sql` |
| Mapping | `mapping_rules.py`, `direct_mapping.py`, `mapping_service.py` |
| Lineage | `company_lineage.py`, `lineage_cli.py` |
| Purge | `company_purge.py`, `company_purge_cli.py` |
| Read projection and reports | `storage/report_projection.py`, `report.py`, `tests/inspect_*.py` |

## 18. Related documents

- [Architecture](architecture.md)
- [UML](uml.md)
- [Project blueprint](designs/project_proposal.txt)
- [Ingestion and update design](designs/ingestAndUpdate.txt)
- [Evidence storage design](designs/evidence-storage.md)
- [Evidence storage runbook](evidence_storage_runbook.md)
- [Direct Mapping design](designs/direct-mapping-implementation-plan.md)

