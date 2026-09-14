import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from sec_inline_financials.storage.config import evidence_runtime_paths
from sec_inline_financials.storage.evidence_store import EvidenceStore

ticker = "AAPL"

paths = evidence_runtime_paths(os.environ)

# Find the company's latest active annual filing snapshot.
with closing(sqlite3.connect(paths.database)) as connection:
    connection.row_factory = sqlite3.Row

    snapshot = connection.execute(
        """
        SELECT
            s.id AS snapshot_id,
            f.accession,
            f.report_date
        FROM evidence_snapshots AS s
        JOIN filings AS f ON f.id = s.filing_id
        JOIN companies AS c ON c.id = f.company_id
        WHERE upper(c.ticker) = upper(?)
          AND f.form = '10-K'
          AND f.is_active = 1
        ORDER BY f.report_date DESC
        LIMIT 1
        """,
        (ticker,),
    ).fetchone()

if snapshot is None:
    raise RuntimeError(f"No stored annual filing found for {ticker}")

snapshot_id = snapshot["snapshot_id"]

# Retrieve every page of facts.
store = EvidenceStore(paths.database, paths.artifacts)
all_facts = []
cursor = 0

while True:
    page = store.list_facts(
        snapshot_id=snapshot_id,
        cursor=cursor,
        limit=1000,
    )
    all_facts.extend(page.items)

    if page.next_cursor is None:
        break

    cursor = page.next_cursor

print(f"Ticker: {ticker}")
print(f"Accession: {snapshot['accession']}")
print(f"Report date: {snapshot['report_date']}")
print(f"Snapshot ID: {snapshot_id}")
print(f"Fact occurrences: {len(all_facts)}")

for fact in all_facts[:10]:
    print(
        fact["display_qname"],
        fact["raw_value_text"],
        fact["period_end_date"],
        fact["legacy_report_unit_text"],
    )

# Optional JSON export.
output_path = Path(f"{ticker}_latest_10k_all_facts.json")
output_path.write_text(
    json.dumps(all_facts, indent=2, default=str),
    encoding="utf-8",
)

print(f"Saved to: {output_path.resolve()}")
