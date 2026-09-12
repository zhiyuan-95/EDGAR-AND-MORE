from __future__ import annotations

import os
import time
from collections.abc import Sequence

from sec_inline_financials.company_ingestion import main as run_company_ingestion
from sec_inline_financials.storage.config import evidence_runtime_paths


def main(argv: Sequence[str] | None = None) -> int:
    """Run the real SEC company-ingestion command."""
    started_at = time.perf_counter()
    exit_code = run_company_ingestion(argv)
    elapsed_seconds = time.perf_counter() - started_at
    paths = evidence_runtime_paths(os.environ)

    print(f"Ingestion time: {elapsed_seconds:.2f} seconds")
    print(f"Evidence storage directory: {paths.root.resolve()}")
    print(f"Inline filing artifacts: {(paths.artifacts / 'objects' / 'sha256').resolve()}")
    print(f"SQLite evidence database: {paths.database.resolve()}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
