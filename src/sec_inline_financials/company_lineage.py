from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Literal, Protocol

from sec_inline_financials.errors import LineageError


def normalize_cik(value: str) -> str:
    stripped = value.strip()
    if not stripped.isdigit() or len(stripped) > 10 or int(stripped) == 0:
        raise LineageError("CIK must be a positive number containing at most 10 digits.")
    return stripped.zfill(10)


@dataclass(frozen=True)
class RegistrantIdentity:
    cik: str
    legal_name: str


@dataclass(frozen=True)
class LineageEdge:
    predecessor_cik: str
    successor_cik: str
    effective_date: date | None = None


@dataclass(frozen=True)
class ReportingTransition:
    predecessor: RegistrantIdentity
    successor: RegistrantIdentity
    effective_date: date


@dataclass(frozen=True)
class CompanyLineage:
    company_id: int
    canonical_current_cik: str
    current_to_oldest: tuple[RegistrantIdentity, ...]
    edges: tuple[LineageEdge, ...]
    state_hash: str


@dataclass(frozen=True)
class LineagePatchPlan:
    company_id: int
    successor: RegistrantIdentity
    predecessor: RegistrantIdentity
    before: CompanyLineage
    after: CompanyLineage
    expected_state_hash: str
    already_present: bool


@dataclass(frozen=True)
class LineageMaintenanceResult:
    disposition: Literal["created", "updated", "already_present"]
    ingestion: object


class RegistrantResolver(Protocol):
    def resolve_registrant(self, cik: str) -> RegistrantIdentity: ...


class LineageStore(Protocol):
    def load_company_lineage(self, cik: str) -> CompanyLineage | None: ...

    def apply_lineage_patch(
        self, plan: LineagePatchPlan
    ) -> Literal["created", "updated", "already_present"]: ...


def build_company_lineage(
    *,
    company_id: int,
    canonical_current_cik: str,
    members: tuple[RegistrantIdentity, ...],
    edges: tuple[LineageEdge, ...],
) -> CompanyLineage:
    canonical = normalize_cik(canonical_current_cik)
    normalized_members = tuple(
        RegistrantIdentity(cik=normalize_cik(member.cik), legal_name=member.legal_name.strip())
        for member in members
    )
    if any(not member.legal_name for member in normalized_members):
        raise LineageError("Stored lineage contains a member without a legal name.")
    by_cik = {member.cik: member for member in normalized_members}
    if len(by_cik) != len(members) or canonical not in by_cik:
        raise LineageError("Stored lineage members are duplicated or omit the canonical CIK.")

    # Edges are predecessor -> successor. Walk backwards from the canonical sink
    # so every member must occur on one unbranched, cycle-free path:
    # oldest C -> B -> A canonical; traversal is A, B, C.
    predecessor_by_successor: dict[str, str] = {}
    successor_by_predecessor: dict[str, str] = {}
    normalized_edge_list: list[LineageEdge] = []
    for edge in edges:
        predecessor = normalize_cik(edge.predecessor_cik)
        successor = normalize_cik(edge.successor_cik)
        if predecessor == successor or predecessor not in by_cik or successor not in by_cik:
            raise LineageError("Stored lineage contains an invalid or disconnected edge.")
        if successor in predecessor_by_successor or predecessor in successor_by_predecessor:
            raise LineageError("Stored lineage contains a branch or merge.")
        predecessor_by_successor[successor] = predecessor
        successor_by_predecessor[predecessor] = successor
        normalized_edge_list.append(LineageEdge(predecessor, successor, edge.effective_date))

    ordered: list[RegistrantIdentity] = []
    seen: set[str] = set()
    cursor: str | None = canonical
    while cursor is not None:
        if cursor in seen:
            raise LineageError("Stored lineage contains a cycle.")
        seen.add(cursor)
        ordered.append(by_cik[cursor])
        cursor = predecessor_by_successor.get(cursor)
    if seen != set(by_cik):
        raise LineageError("Stored lineage contains disconnected members.")

    normalized_edges = tuple(
        sorted(
            normalized_edge_list,
            key=lambda item: (item.successor_cik, item.predecessor_cik),
        )
    )
    serialized = json.dumps(
        {
            "canonical_current_cik": canonical,
            "members": [member.cik for member in ordered],
            "edges": [
                [
                    edge.predecessor_cik,
                    edge.successor_cik,
                    edge.effective_date.isoformat() if edge.effective_date is not None else None,
                ]
                for edge in normalized_edges
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    return CompanyLineage(
        company_id=company_id,
        canonical_current_cik=canonical,
        current_to_oldest=tuple(ordered),
        edges=normalized_edges,
        state_hash=hashlib.sha256(serialized.encode("utf-8")).hexdigest(),
    )


class LineageMaintenanceService:
    def __init__(
        self,
        *,
        store: LineageStore,
        registrant_resolver: RegistrantResolver,
        ingest_company_cik: Callable[[str], object] | None = None,
    ) -> None:
        self._store = store
        self._registrant_resolver = registrant_resolver
        self._ingest_company_cik = ingest_company_cik

    def preview_link(
        self,
        successor_cik: str,
        predecessor_cik: str,
        *,
        effective_date: date | None = None,
    ) -> LineagePatchPlan:
        successor_key = normalize_cik(successor_cik)
        predecessor_key = normalize_cik(predecessor_cik)
        if successor_key == predecessor_key:
            raise LineageError("Successor and predecessor CIKs must be different.")

        before = self._store.load_company_lineage(successor_key)
        if before is None:
            raise LineageError(
                f"Successor CIK {successor_key} does not belong to a stored company lineage."
            )
        stored_edge = next(
            (
                edge
                for edge in before.edges
                if edge.predecessor_cik == predecessor_key and edge.successor_cik == successor_key
            ),
            None,
        )
        already_present = stored_edge is not None
        if (
            stored_edge is not None
            and stored_edge.effective_date is not None
            and effective_date is not None
            and stored_edge.effective_date != effective_date
        ):
            raise LineageError(
                "The transition effective date is already verified and cannot be changed."
            )
        target_effective_date = (
            effective_date
            if effective_date is not None
            else stored_edge.effective_date
            if stored_edge is not None
            else None
        )
        exact_edge = LineageEdge(predecessor_key, successor_key, target_effective_date)
        predecessor_owner = self._store.load_company_lineage(predecessor_key)
        if predecessor_owner is not None:
            if predecessor_owner.company_id != before.company_id:
                raise LineageError(
                    f"Predecessor CIK {predecessor_key} belongs to another stored company."
                )
            if not already_present:
                raise LineageError(
                    f"CIK {predecessor_key} is already attached elsewhere in this lineage."
                )
        if not already_present and before.current_to_oldest[-1].cik != successor_key:
            raise LineageError(
                "The supplied successor must be the oldest member of the existing lineage."
            )

        successor = self._resolved_identity(successor_key)
        predecessor = self._resolved_identity(predecessor_key)
        if already_present:
            after = build_company_lineage(
                company_id=before.company_id,
                canonical_current_cik=before.canonical_current_cik,
                members=before.current_to_oldest,
                edges=tuple(
                    exact_edge
                    if edge.predecessor_cik == predecessor_key
                    and edge.successor_cik == successor_key
                    else edge
                    for edge in before.edges
                ),
            )
        else:
            members = tuple(
                successor if member.cik == successor_key else member
                for member in before.current_to_oldest
            ) + (predecessor,)
            after = build_company_lineage(
                company_id=before.company_id,
                canonical_current_cik=before.canonical_current_cik,
                members=members,
                edges=(*before.edges, exact_edge),
            )
        return LineagePatchPlan(
            company_id=before.company_id,
            successor=successor,
            predecessor=predecessor,
            before=before,
            after=after,
            expected_state_hash=before.state_hash,
            already_present=already_present,
        )

    def apply_link(
        self, plan: LineagePatchPlan
    ) -> Literal["created", "updated", "already_present"]:
        return self._store.apply_lineage_patch(plan)

    def apply_and_ingest(self, plan: LineagePatchPlan) -> LineageMaintenanceResult:
        disposition = self.apply_link(plan)
        if self._ingest_company_cik is None:
            raise LineageError("Lineage ingestion is not configured.")
        ingestion = self._ingest_company_cik(plan.before.canonical_current_cik)
        return LineageMaintenanceResult(disposition=disposition, ingestion=ingestion)

    def _resolved_identity(self, cik: str) -> RegistrantIdentity:
        identity = self._registrant_resolver.resolve_registrant(cik)
        if normalize_cik(identity.cik) != cik:
            raise LineageError(f"SEC registrant response did not match requested CIK {cik}.")
        legal_name = identity.legal_name.strip()
        if not legal_name:
            raise LineageError(f"SEC registrant response for CIK {cik} has no legal name.")
        return RegistrantIdentity(cik=cik, legal_name=legal_name)
