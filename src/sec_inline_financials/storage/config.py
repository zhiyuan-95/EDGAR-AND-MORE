from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

_DATA_ROOT_VARIABLE = "SEC_INLINE_FINANCIALS_DATA_DIR"


@dataclass(frozen=True)
class EvidenceRuntimePaths:
    root: Path
    database: Path
    artifacts: Path


def evidence_runtime_paths(environment: Mapping[str, str]) -> EvidenceRuntimePaths:
    """Resolve the external evidence runtime root without reading project files."""
    configured = environment.get(_DATA_ROOT_VARIABLE, "").strip()
    if configured:
        root = Path(configured).expanduser()
    else:
        local_app_data = environment.get("LOCALAPPDATA", "").strip()
        if not local_app_data:
            raise ValueError(f"LOCALAPPDATA is unavailable; set {_DATA_ROOT_VARIABLE} explicitly.")
        root = Path(local_app_data) / "SECInlineFinancials" / "data"
    return EvidenceRuntimePaths(
        root=root,
        database=root / "evidence.sqlite3",
        artifacts=root,
    )
