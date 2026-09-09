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

user_agent = load_sec_user_agent(
    working_directory=Path.cwd(),
    environment=os.environ,
)

service = EvidenceIngestionService(
    sec_client=SecClient(user_agent=user_agent),
    processor=ArelleProcessor(user_agent=user_agent),
    store=store,
)

outcome = service.ingest_company_window(
    "MSFT",
    annual_count=5,
    quarterly_count=12,
)

print("Run:", outcome.run_id, outcome.status)
for filing in outcome.filings:
    print(
        filing.accession,
        filing.status,       # stored, reused, or failed
        filing.snapshot_id,
        filing.error,
    )
