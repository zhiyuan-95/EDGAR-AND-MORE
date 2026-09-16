from __future__ import annotations

import argparse
import os
from collections.abc import Sequence

from sec_inline_financials.errors import ExplorerError
from sec_inline_financials.mapping_models import MetricEvaluationRef
from sec_inline_financials.mapping_service import DirectMappingService
from sec_inline_financials.storage.config import evidence_runtime_paths
from sec_inline_financials.storage.evidence_store import EvidenceStore


def _evaluation_line(label: str, evaluation: MetricEvaluationRef) -> str:
    disposition = "reused" if evaluation.reused else "created"
    return (
        f"{label} evaluation {evaluation.evaluation_id}: "
        f"{evaluation.reported_count} reported, {evaluation.missing_count} missing "
        f"({disposition})"
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate Direct Mapping from retained filing evidence."
    )
    parser.add_argument("ticker")
    args = parser.parse_args(argv)
    try:
        paths = evidence_runtime_paths(os.environ)
        store = EvidenceStore(paths.database, paths.artifacts)
        result = DirectMappingService(store).evaluate_company(args.ticker)
    except (ExplorerError, ValueError) as exc:
        print(f"Error: {exc}")
        return 1
    print(f"{result.company.ticker}: direct mapping complete")
    print(_evaluation_line("Annual", result.annual))
    print(_evaluation_line("Quarterly", result.quarterly))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
