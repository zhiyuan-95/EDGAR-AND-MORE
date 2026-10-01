# Evidence Storage Runbook

Evidence ingestion is separate from stored-evidence report generation. Ingestion
never renders or writes a Showcase Report. Reports are generated interactively with
`uv run --no-sync python tests/inspect_inline_ingestion.py`. Narrative sections are
inspected separately with `uv run --no-sync python tests/inspect_filings.py`. The two
ingestion console commands do not generate reports; the separate
`sec-inline-financials-map` command evaluates Direct Mapping from stored evidence
only.

## Runtime location

By default, runtime data is stored under:

```text
%LOCALAPPDATA%/SECInlineFinancials/data/
  evidence.sqlite3
  objects/sha256/<first-two-hash-characters>/<full-sha256>
  staging/<attempt-id>/
  cache/arelle/
  cache/sec-transform-<pinned-commit-prefix>/
```

Set `SEC_INLINE_FINANCIALS_DATA_DIR` to use another root. The Python path resolver
is `sec_inline_financials.storage.config.evidence_runtime_paths`. Tests and tools
should always inject a temporary or explicitly selected root.

Do not place the live database in the source checkout. Do not copy only an open
`evidence.sqlite3` file: WAL state may contain committed work.

## Initialization and ingestion

For the production request-triggered workflow, run:

```powershell
uv run --no-sync sec-inline-financials AAPL
```

This command writes flushed stage/per-filing progress to stderr. It writes the final
company and run summaries, elapsed time, evidence root, content-addressed artifact
directory, and SQLite path to stdout. Use `--annual-count`, `--quarterly-count`, and
`--force` when needed.

The lower-level aliases omit the final timing and path lines:

```powershell
uv run --no-sync sec-inline-financials-ingest AAPL
uv run --no-sync python -m sec_inline_financials.company_ingestion AAPL
```

For programmatic request-triggered ingestion, use the same public service as the
CLI:

```python
from pathlib import Path

from sec_inline_financials.company_ingestion import IngestionSettings, ingest_company

settings = IngestionSettings.from_environment(working_directory=Path.cwd())
result = ingest_company("MSFT", settings, progress=print)
```

The result is `reused_local` without SEC or Arelle when the active window is complete
and its next-check dates are current. A due/forced request, missing evidence, or an
active legacy snapshot without narrative sections enters SEC discovery and processes
the selected window. Legacy `evidence-v1` snapshots remain immutable; the current
profile stores new `evidence-v2` snapshots with `filing_sections`.

When processing occurs, `result.run` contains a run ID and one stored, reused, or
failed outcome per filing. Each filing commits independently. A failed filing cannot
expose a partial snapshot, and earlier completed filings remain usable.

## CIK lineage maintenance

Use the lineage command only when the first CIK already belongs to a stored company
and the second CIK is its exact legal predecessor:

```powershell
uv run --no-sync sec-inline-financials-lineage SUCCESSOR_CIK PREDECESSOR_CIK
```

The command obtains the legal name for both CIKs from the SEC, prints the canonical
company, complete existing lineage, and exact proposed predecessor-to-successor edge,
then asks for `y/n`. Only `y` or `Y` saves the edge and starts ingestion. The two CIKs
remain distinct registrants; filing provenance records the registrant CIK and SEC
archive-owner CIK for every stored accession.

Discovery searches the canonical current CIK first and then walks the stored lineage
toward older predecessors. A predecessor fills only an annual-year or quarterly-date
slot absent from every higher-priority registrant. A selected current filing that
fails processing is reported as a failure; it is not replaced by a same-period
predecessor filing. Only filings with completed evidence snapshots can enter the
published active window.

For a chain `B -> A`, add older predecessor `C` by supplying `B C`; `B` is the current
oldest member. Exact-edge reruns are idempotent and retry ordinary ingestion. If
ingestion fails after approval, the edge intentionally remains committed, so rerun
the same command and approve it again. Version one has no unlink command. Back up and
audit an important evidence root before creating a relationship; correcting a wrong
edge requires restoring that backup or a separately reviewed repair procedure.

The command fills the configured five-annual/twelve-quarterly window. It does not
download the predecessor's complete SEC archive.

Arelle uses the pinned SEC transformation plugin plus a local retry plugin. The
retry is limited to HTTP 503 responses for HTTPS filing extension-taxonomy `.xsd`
resources below `sec.gov/Archives/edgar/data/` or
`www.sec.gov/Archives/edgar/data/`, with delays of 1, 2, and 4 seconds. Permanent
errors, filing documents, and non-SEC hosts are not retried by this layer.

## Stored-evidence inspectors

Generate an annual or quarterly report plus a complete-facts JSON export:

```powershell
uv run --no-sync python tests/inspect_inline_ingestion.py
```

Inspect selected narrative sections across stored 10-K or 10-Q periods:

```powershell
uv run --no-sync python tests/inspect_filings.py
```

Both workflows are interactive and write user-requested artifacts under `output/`.
Neither contacts the SEC nor runs Arelle. The narrative inspector opens SQLite in
query-only mode, verifies the retained primary-document artifact, and rebuilds the
selected section text without modifying stored rows.

## Retrieval and integrity

All retrieval starts from an explicit snapshot ID. Use `list_concepts`,
`list_filing_sections`, `list_facts`, `get_fact`, `get_conflict`,
`list_calculation_children`, and `list_validation_messages` for bounded queries.
Use `load_snapshot` only for full
round-trip or replay work.

`audit_snapshot` checks linked row counts, every referenced artifact's SHA-256 and
size, and SQLite foreign keys. `resolve_artifact` verifies the object again before
returning its local path. Missing or changed bytes are integrity failures; the
database record is preserved for diagnosis.

## Compaction, deduplication, and retention contract

New string-valued facts are stored losslessly without duplicating identical text
across `facts.raw_value_text` and `facts.typed_value_text`. `EvidenceStore` stores
`typed_value_text` as `NULL` only when `typed_value_kind = 'string'` and the typed
text is exactly equal to the raw text. `load_snapshot`, `list_facts`, `get_fact`,
and mapping-result reads reconstruct the typed value from the raw value. If the
two strings differ, or the typed kind is numeric, date, QName, XML, or another
kind, both representations are retained.

Schema migration `0006_compressed_text_payloads.sql` adds the content-addressed
`text_payloads` table and `facts.raw_value_payload_sha256`. On new writes, a
nonnumeric string raw value is payload-backed only when its UTF-8 representation
is at least 1 KiB and zlib compression is smaller than the original. The payload
row records its original SHA-256, UTF-8 encoding, codec, original/compressed byte
sizes, compressed bytes, and creation time. Small, numeric, and incompressible
values remain inline. `load_snapshot`, bounded fact reads, conflict reads, mapping
candidate reads, and metric-result reads reconstruct values through
`EvidenceStore`; direct SQLite consumers must not assume `raw_value_text` is
non-NULL when `raw_value_payload_sha256` is present.

Snapshot audits decompress every referenced text payload and verify its compressed
size, original size, UTF-8 encoding, and SHA-256. Corruption is an explicit storage
error. Company purge removes only payloads that have no remaining fact reference.
The migration is forward-write only: it does not rewrite existing inline values or
run `VACUUM` against an existing store.

This is representation compaction, not semantic fact deduplication. Every
Observed Filing Fact remains a separate row even when its displayed value equals
another row. Period, dimensions, unit, validity, source location, and conflict
lineage can make equal-looking occurrences materially different.

Immutable artifacts use a separate rule: identical SHA-256 content shares one
physical object under `objects/sha256`, while every snapshot and processing
attempt keeps its own reference. Objects with different bytes are never merged.
An artifact is eligible for deletion only after no retained snapshot, source
document, or processing attempt references it.

Artifact objects remain stored as their exact original bytes. Migration 0006 does
not add artifact-file compression because `ArtifactStore.resolve()` currently
returns a verified path to those exact bytes; changing that contract requires a
separate cache/materialization design and acceptance test.

Committed history is retained by default. The active five-10-K/twelve-10-Q Filing
Window is a publication selection, not a deletion threshold. Refreshes do not
prune older snapshots, facts, sections, evaluations, attempts, or artifacts. The
implemented deletion path is the explicit company-scoped purge below: preview is
the default, `--execute` is required, active ingestion blocks the operation, and
shared artifacts survive.

Rows written before string compaction remain fully readable but are not rewritten
automatically. Reclaiming their existing duplicate column payload is a separate
maintenance operation: stop ingestion, create and audit a backup, produce a
read-only estimate, update only exact string duplicates, run integrity and replay
checks, and use SQLite `VACUUM` only after confirming sufficient temporary disk
space. No automatic selective-history pruning or historical rewrite is currently
implemented.

## Restart recovery

`recover_interrupted_attempts(store)` marks work owned by a process that is no longer
alive as interrupted. The current production CLI does not call it automatically;
invoke it explicitly before manual recovery work. It defers attempts owned by a live
process and does not delete staging directories, committed artifacts, or unreferenced
immutable objects.

Retrying an accession with the same extraction profile reuses a compatible,
artifact-verified snapshot. Reuse means local evidence was reused; it is not a
claim that SEC bytes were checked again.

## Company-scoped purge

The purge command accepts one ticker, a comma-separated set, or a space-separated
set. It defaults to a non-deleting preview (normal schema initialization may still
apply a packaged migration):

```powershell
uv run --no-sync sec-inline-financials-purge AAPL
uv run --no-sync sec-inline-financials-purge AAPL,MSFT,NVDA
```

Review the resolved company names/CIKs and counts, then explicitly execute:

```powershell
uv run --no-sync sec-inline-financials-purge AAPL --execute
uv run --no-sync sec-inline-financials-purge AAPL MSFT NVDA --execute
```

Execution is all-or-nothing for the requested ticker set. An unknown ticker aborts
the whole request. The database transaction removes company, filing, run, attempt,
snapshot, section, fact, evaluation, mapping, and link rows. It deletes global
concept/artifact records only after they become unreferenced. Any artifact shared
with an unselected company remains in both SQLite and `objects/sha256`.

The command requires quiescent ingestion. If an abandoned run is still marked
`running`, invoke `recover_interrupted_attempts(store)` as described above and then
retry. The purge removes per-attempt Arelle cache and capture data under
`staging/<attempt-id>`, but preserves the shared pinned SEC transform-plugin cache.
It also preserves manually generated `output/` reports, because those are
user-requested exports rather than database-attached evidence.

Filesystem deletion is recorded in `pending_file_deletions` in the same SQLite
transaction. If Windows has a file locked, the company database purge remains
committed and the path remains queued. Close the process holding the file and run:

```powershell
uv run --no-sync sec-inline-financials-purge --cleanup-pending
```

For an important dataset, create and audit a backup before using `--execute`.

## Backup and restore verification

Stop ingestion, then call:

```python
from pathlib import Path

from sec_inline_financials.storage.recovery import audit_backup, backup_evidence

manifest = backup_evidence(store, Path("D:/SECInlineFinancials-backup"))
result = audit_backup(manifest.parent)
assert result["ok"]
```

`backup_evidence` refuses to run while an ingestion run is active and refuses to
overwrite an existing destination. It uses SQLite's backup interface, copies every
snapshot- or attempt-referenced immutable object, and writes a hash manifest.
`audit_backup` verifies the database hash, migrations, snapshot links, and retained
objects from the restored root.

## Verification commands

```powershell
uv run --no-sync pytest -q
uv run --no-sync ruff check src tests
uv run --no-sync ruff format --check src tests
uv run --no-sync mypy src
```

The active automated suite covers narrative-section extraction/replay, lossless
text-payload compaction and purge cleanup, and the scoped SEC taxonomy retry. This
checkout does not contain an opt-in live pytest. Recorded production CLI runs have
verified complete 5/12 evidence-v2 windows, narrative-section persistence, and
artifact audits, but a live update that discovers a genuinely new accession remains
a separate acceptance case. For every live acceptance, record accessions, extraction
profiles, counts, hashes, elapsed time, database size, artifact size, largest text
fact, and peak memory without recording `SEC_USER_AGENT` or other
contact/configuration values.
