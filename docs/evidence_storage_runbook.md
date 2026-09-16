# Evidence Storage Runbook

Evidence ingestion is separate from stored-evidence report generation. Ingestion
never renders or writes a Showcase Report. Reports are generated interactively with
`uv run --no-sync python tests/inspect_ingestion.py`. The two ingestion console
commands do not generate reports; the separate `sec-inline-financials-map` command
evaluates Direct Mapping from stored evidence only.

## Runtime location

By default, runtime data is stored under:

```text
%LOCALAPPDATA%/SECInlineFinancials/data/
  evidence.sqlite3
  objects/sha256/<first-two-hash-characters>/<full-sha256>
  staging/<attempt-id>/
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

## Restart recovery

`recover_interrupted_attempts(store)` marks work owned by a process that is no longer
alive as interrupted. The current production CLI does not call it automatically;
invoke it explicitly before manual recovery work. It defers attempts owned by a live
process and does not delete staging directories, committed artifacts, or unreferenced
immutable objects.

Retrying an accession with the same extraction profile reuses a compatible,
artifact-verified snapshot. Reuse means local evidence was reused; it is not a
claim that SEC bytes were checked again.

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

The opt-in pytest live test exercises complete detached evidence extraction, not
database persistence or stored report replay. Recorded production CLI runs have
verified complete 5/12 evidence-v2 windows, narrative-section persistence, and
artifact audits. A live update that discovers a genuinely new accession remains a
separate acceptance case. For every live acceptance, record accessions, extraction
profiles, counts, hashes, elapsed time, database size, artifact size, largest text
fact, and peak memory without recording `SEC_USER_AGENT` or other
contact/configuration values.
