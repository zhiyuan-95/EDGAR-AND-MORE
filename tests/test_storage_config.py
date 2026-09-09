from pathlib import Path

import pytest

from sec_inline_financials.storage.config import evidence_runtime_paths


def test_runtime_paths_default_outside_the_checkout() -> None:
    paths = evidence_runtime_paths({"LOCALAPPDATA": "C:/Users/example/AppData/Local"})

    assert paths.root == Path("C:/Users/example/AppData/Local/SECInlineFinancials/data")
    assert paths.database == paths.root / "evidence.sqlite3"
    assert paths.artifacts == paths.root


def test_runtime_paths_accept_explicit_override_and_require_a_root() -> None:
    assert evidence_runtime_paths({"SEC_INLINE_FINANCIALS_DATA_DIR": "D:/evidence"}).root == Path(
        "D:/evidence"
    )
    with pytest.raises(ValueError, match="LOCALAPPDATA"):
        evidence_runtime_paths({})
