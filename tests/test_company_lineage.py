from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

import pytest

from sec_inline_financials.company_lineage import (
    LineageError,
    LineageMaintenanceService,
    RegistrantIdentity,
)
from sec_inline_financials.storage.evidence_store import EvidenceStore

DatabaseAtVersionFactory = Callable[[int], Path]


class StaticRegistrantResolver:
    def __init__(self, identities: dict[str, str]) -> None:
        self._identities = identities

    def resolve_registrant(self, cik: str) -> RegistrantIdentity:
        return RegistrantIdentity(cik=cik, legal_name=self._identities[cik])


def _insert_company(database_path: Path, *, cik: str, name: str) -> None:
    with sqlite3.connect(database_path) as connection:
        now = "2026-01-01T00:00:00+00:00"
        cursor = connection.execute(
            "INSERT INTO companies(cik, ticker, current_name, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (cik, "TEST", name, now, now),
        )
        company_id = int(cursor.lastrowid)
        connection.execute(
            "INSERT INTO company_ciks(company_id, cik, legal_name, associated_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (company_id, cik, name, now, now),
        )


def test_apply_and_ingest_without_ingestion_callback_does_not_commit_lineage(
    database_at_version: DatabaseAtVersionFactory,
) -> None:
    database_path = database_at_version(8)
    successor_cik = "0000000001"
    predecessor_cik = "0000000002"
    _insert_company(database_path, cik=successor_cik, name="Successor Corp")
    store = EvidenceStore(database_path, database_path.parent)
    service = LineageMaintenanceService(
        store=store,
        registrant_resolver=StaticRegistrantResolver(
            {
                successor_cik: "Successor Corp",
                predecessor_cik: "Predecessor Corp",
            }
        ),
    )

    plan = service.preview_link(successor_cik, predecessor_cik)

    with pytest.raises(LineageError, match="Lineage ingestion is not configured"):
        service.apply_and_ingest(plan)

    with sqlite3.connect(database_path) as connection:
        predecessor_count = connection.execute(
            "SELECT count(*) FROM company_ciks WHERE cik = ?",
            (predecessor_cik,),
        ).fetchone()[0]
        transition_count = connection.execute(
            "SELECT count(*) FROM company_cik_transitions",
        ).fetchone()[0]

    assert predecessor_count == 0
    assert transition_count == 0
