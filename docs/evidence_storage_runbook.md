# Evidence Storage Runbook

The evidence-storage implementation is a separate workflow from the existing TXT
report command. Ingestion never renders or writes a Showcase Report.

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

Create the store, apply packaged checksum-verified migrations, then construct the
explicit ingestion module with the existing SEC and Arelle adapters:

```python
import os
from pathlib import Path

from sec_inline_financials.arelle_adapter import ArelleProcessor
from sec_inline_financials.config import load_sec_user_agent
from sec_inline_financials.evidence_ingestion import EvidenceIngestionService
from sec_inline_financials.sec_client import SecClient
from sec_inline_financials.storage.config import evidence_runtime_paths
from sec_inline_financials.storage.evidence_store import EvidenceStore

paths = evidence_runtime_paths(os.environ)
store = EvidenceStore(paths.database, paths.artifacts)
store.initialize()

user_agent = load_sec_user_agent(working_directory=Path.cwd(), environment=os.environ)
service = EvidenceIngestionService(
    sec_client=SecClient(user_agent=user_agent),
    processor=ArelleProcessor(user_agent=user_agent),
    store=store,
)
outcome = service.ingest_company_window("MSFT", annual_count=5, quarterly_count=12)
```

The result contains a run ID and one stored, reused, or failed outcome per filing.
Each filing commits independently. A failed filing cannot expose a partial snapshot,
and earlier completed filings remain usable.

## Retrieval and integrity

All retrieval starts from an explicit snapshot ID. Use `list_concepts`,
`list_facts`, `get_fact`, `get_conflict`, `list_calculation_children`, and
`list_validation_messages` for bounded queries. Use `load_snapshot` only for full
round-trip or replay work.

`audit_snapshot` checks linked row counts, every referenced artifact's SHA-256 and
size, and SQLite foreign keys. `resolve_artifact` verifies the object again before
returning its local path. Missing or changed bytes are integrity failures; the
database record is preserved for diagnosis.

## Restart recovery

Call `recover_interrupted_attempts(store)` at application startup. It marks work
owned by a process that is no longer alive as interrupted. It defers attempts owned
by a live process and does not delete staging directories, committed artifacts, or
unreferenced immutable objects.

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

Real SEC acceptance remains opt-in. Record accessions, extraction profiles, counts,
hashes, elapsed time, database size, artifact size, largest text fact, and peak
memory without recording `SEC_USER_AGENT` or other contact/configuration values.
