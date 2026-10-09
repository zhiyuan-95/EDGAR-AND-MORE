from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from sec_inline_financials.mapping_models import MappingSnapshotInput, active_window_hash


@pytest.mark.parametrize(
    ("field_name", "changed_value"),
    (
        ("registrant_cik", "0000000002"),
        ("registrant_ciks", ("0000000001", "0000000002")),
    ),
)
def test_active_window_hash_changes_when_registrant_identity_changes(
    field_name: str,
    changed_value: object,
) -> None:
    original = _snapshot(snapshot_id=1, rank=1)
    changed = replace(original, **{field_name: changed_value})

    assert active_window_hash((changed,)) != active_window_hash((original,))


def test_active_window_hash_normalizes_registrant_and_snapshot_order() -> None:
    first = _snapshot(
        snapshot_id=1,
        rank=1,
        registrant_ciks=("0000000002", "0000000001"),
    )
    second = _snapshot(snapshot_id=2, rank=2)
    normalized_first = replace(
        first,
        registrant_ciks=("0000000001", "0000000002"),
    )

    assert active_window_hash((first, second)) == active_window_hash((second, normalized_first))


def _snapshot(
    *,
    snapshot_id: int,
    rank: int,
    registrant_ciks: tuple[str, ...] = ("0000000001",),
) -> MappingSnapshotInput:
    return MappingSnapshotInput(
        snapshot_id=snapshot_id,
        filing_id=snapshot_id,
        accession=f"0000000001-26-{snapshot_id:06d}",
        form="10-K",
        report_date=date(2025 - rank, 12, 31),
        active_window_rank=rank,
        payload_hash=f"payload-{snapshot_id}",
        report_evaluation_id=snapshot_id,
        source_report_rule_version="report-v2",
        company_cik="0000000001",
        registrant_cik="0000000001",
        registrant_ciks=registrant_ciks,
    )
