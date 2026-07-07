"""Shared-memory grants (R2/ADR-016): namespace A lets namespace B read.

A grant is an ordinary record through the write door — new information rides
the log like everything else (D0.1):

- ``namespace``   = the GRANTOR (the grant lives with what it exposes),
- ``memory_type`` = ``"shared"`` (bookkeeping records — they never cross a
  grant themselves, see :func:`memspine.core.namespace.grant_allows`),
- ``entity``      = the grantee namespace,
- ``attribute``   = ``"grant"``,
- ``content``     = canonical JSON documenting the scope (human-readable AND
  machine-parseable; the engine builds it — never caller free text, which is
  the stated firewall exemption shared with prompt sync, ADR-014 §2).

Revocation archives the grant via a delta DECAY_TRANSITION (P3.1 rule); the
enforcement decision itself lives in ``core/namespace.grant_allows`` — the
ONE place a cross-namespace read is allowed or refused.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import ClassVar, Protocol

import orjson

from memspine.core.events import EventKind, MemoryEvent, canonical_payload
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.core.registry import MEMORY_TYPES
from memspine.exceptions import ConflictError
from memspine.memories.base import BaseMemory
from memspine.memories.shared.subscriptions import active_subscriptions
from memspine.observability.logging import EVENT_DECAY_TRANSITION, EVENT_WRITE, get_logger

__all__ = [
    "GRANT_ATTRIBUTE",
    "Grant",
    "SharedMemory",
    "active_grants",
    "make_grant_record",
    "parse_grant",
]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]

#: The ``attribute`` value that marks a shared record as a grant.
GRANT_ATTRIBUTE = "grant"


@dataclass(frozen=True)
class Grant:
    """One live read grant: ``grantor`` lets ``grantee`` read its records,
    optionally scoped to ``memory_types`` (``None`` = every type)."""

    grantor: str
    grantee: str
    memory_types: frozenset[str] | None
    record_id: str


class SharedStore(Protocol):
    """The storage slice shared memory needs (ports & adapters, D-22)."""

    async def list_namespaces(self) -> list[str]: ...

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...


def _scope_payload(memory_types: frozenset[str] | None) -> list[str] | None:
    return sorted(memory_types) if memory_types is not None else None


def make_grant_record(
    grantor: str,
    grantee: str,
    *,
    memory_types: frozenset[str] | None = None,
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """A new grant record. Content is deterministic canonical JSON — the
    machine-readable scope IS the human-readable documentation."""
    scope = {
        "grant": {"from": grantor, "to": grantee, "memory_types": _scope_payload(memory_types)}
    }
    return MemoryRecord(
        namespace=grantor,
        memory_type="shared",
        content=canonical_payload(scope).decode("utf-8"),
        entity=grantee,
        attribute=GRANT_ATTRIBUTE,
        source=source or SourceInfo(role="user", channel="grant"),
    )


def parse_grant(record: MemoryRecord) -> Grant | None:
    """The grant a record carries, or None when it is not grant-shaped."""
    if record.memory_type != "shared" or record.attribute != GRANT_ATTRIBUTE:
        return None
    if record.entity is None:
        return None
    try:
        raw = orjson.loads(record.content).get("grant", {})
    except orjson.JSONDecodeError:
        return None
    types = raw.get("memory_types")
    scope = frozenset(str(name) for name in types) if types is not None else None
    return Grant(
        grantor=record.namespace,
        grantee=record.entity,
        memory_types=scope,
        record_id=record.record_id,
    )


def _is_live(record: MemoryRecord) -> bool:
    return record.status is RecordStatus.ACTIVATED and not record.quarantined


def active_grants(records: list[MemoryRecord], *, grantee: str | None = None) -> list[Grant]:
    """Live grants among ``records`` (one grantor namespace's shared records),
    optionally filtered to one grantee. Quarantined or archived grant records
    convey nothing (defense in depth, E1)."""
    grants: list[Grant] = []
    for record in records:
        if not _is_live(record):
            continue
        grant = parse_grant(record)
        if grant is None:
            continue
        if grantee is not None and grant.grantee != grantee:
            continue
        grants.append(grant)
    return grants


class SharedMemory(BaseMemory):
    name: ClassVar[str] = "shared"
    #: Hard deps: none — the disjunction below is what the registry enforces.
    needs: ClassVar[tuple[str, ...]] = ()
    #: Any-of dependency (mirrors ``core.registry._REQUIRES_ANY``): shared
    #: memory needs at least one substantive store to share.
    needs_any: ClassVar[tuple[str, ...]] = ("semantic", "episodic")

    def __init__(self, storage: SharedStore, append_event: AppendEvent) -> None:
        self._storage = storage
        self._append_event = append_event

    # ── grants ───────────────────────────────────────────────────────────────

    async def grant(
        self,
        grantor: str,
        grantee: str,
        *,
        memory_types: list[str] | None = None,
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """Grant ``grantee`` read access to ``grantor``'s records.

        One live grant per (grantor, grantee): re-granting an identical scope
        is idempotent (returns the existing record, appends nothing);
        re-granting a different scope supersedes the old grant (archived with
        ``evolve_to`` chaining, D-42) — never two live grants that a reader
        would have to merge.
        """
        if grantor == grantee:
            raise ConflictError(f"namespace {grantor!r} cannot grant read access to itself")
        scope = self._validate_scope(memory_types)
        existing = await self._live_grant_records(grantor, grantee)
        new = make_grant_record(grantor, grantee, memory_types=scope, source=source)
        for record in existing:
            held = parse_grant(record)
            if held is not None and held.memory_types == scope:
                return record  # idempotent: identical scope already granted
        await self._append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=grantor,
                actor=new.source.role,
                payload={"record": new.model_dump(mode="json")},
            )
        )
        for record in existing:  # scope changed: the fresh grant supersedes
            await self._archive_grant(record, reason="grant_superseded", evolve_to=new.record_id)
        _log.info(
            EVENT_WRITE,
            namespace=grantor,
            record_id=new.record_id,
            grant=True,
            grantee=grantee,
            memory_types=_scope_payload(scope),
        )
        return new

    async def revoke(self, grantor: str, grantee: str) -> MemoryRecord:
        """Revoke ``grantee``'s access: the grant record is archived via a
        delta event. Raises when there is nothing to revoke — a typo'd grantee
        must not read as success."""
        existing = await self._live_grant_records(grantor, grantee)
        if not existing:
            raise ConflictError(f"no active grant from {grantor!r} to {grantee!r}")
        revoked = existing[-1]
        for record in existing:
            await self._archive_grant(record, reason="grant_revoked")
        return revoked.model_copy(update={"status": RecordStatus.ARCHIVED})

    async def grants_to(self, grantee: str) -> dict[str, frozenset[str] | None]:
        """Every namespace ``grantee`` may read from, mapped to its scope
        (``None`` = every type) — the exact mapping
        :func:`memspine.core.namespace.grant_allows` consumes. Scopes from
        multiple live grants by one grantor union (an unscoped grant wins)."""
        allowed: dict[str, frozenset[str] | None] = {}
        for namespace in await self._storage.list_namespaces():
            if namespace == grantee:
                continue
            grants = active_grants(
                await self._storage.list_records(namespace, "shared"), grantee=grantee
            )
            if not grants:
                continue
            scope: frozenset[str] | None = frozenset()
            for grant in grants:
                if grant.memory_types is None:
                    scope = None
                    break
                assert scope is not None
                scope = scope | grant.memory_types
            allowed[namespace] = scope
        return allowed

    async def grants_from(self, grantor: str) -> list[Grant]:
        """The live grants ``grantor`` has issued (operator listing surface)."""
        return active_grants(await self._storage.list_records(grantor, "shared"))

    # ── subscriptions (v0.1 minimal, ADR-016) ────────────────────────────────

    async def subscriptions(self, namespace: str) -> list[MemoryRecord]:
        """Live standing-query records in ``namespace`` — callers feed each
        one's ``content`` to ``shared_search`` (pull-based; push delivery is
        deferred to the taskiq build, ADR-016)."""
        return active_subscriptions(await self._storage.list_records(namespace, "shared"))

    # ── internals ────────────────────────────────────────────────────────────

    @staticmethod
    def _validate_scope(memory_types: list[str] | None) -> frozenset[str] | None:
        if memory_types is None:
            return None
        if not memory_types:
            raise ConflictError(
                "an empty memory_types scope grants nothing — pass None for every type "
                "or name at least one"
            )
        unknown = set(memory_types) - MEMORY_TYPES
        if unknown:
            raise ConflictError(
                f"unknown memory type(s) in grant scope: {sorted(unknown)} — "
                f"valid: {sorted(MEMORY_TYPES)}"
            )
        if "shared" in memory_types:
            raise ConflictError(
                "shared bookkeeping records (grants, subscriptions) never cross a grant "
                "(ADR-016) — granting the 'shared' type would be inert"
            )
        return frozenset(memory_types)

    async def _live_grant_records(self, grantor: str, grantee: str) -> list[MemoryRecord]:
        records = [
            record
            for record in await self._storage.list_records(grantor, "shared")
            if _is_live(record) and record.attribute == GRANT_ATTRIBUTE and record.entity == grantee
        ]
        records.sort(key=lambda record: (record.recorded_at, record.record_id))
        return records

    async def _archive_grant(
        self, record: MemoryRecord, *, reason: str, evolve_to: str | None = None
    ) -> None:
        now = datetime.now(UTC)
        change: dict[str, object] = {
            "status": RecordStatus.ARCHIVED.value,
            "valid_to": now.isoformat(),
            "superseded_at": now.isoformat(),
        }
        if evolve_to is not None:
            change["evolve_to"] = evolve_to
        await self._append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=record.namespace,
                actor="system",
                payload={
                    "record_id": record.record_id,
                    "set": change,
                    "transition": "grant->archived",
                    "reason": reason,
                },
            )
        )
        _log.info(
            EVENT_DECAY_TRANSITION,
            namespace=record.namespace,
            record_id=record.record_id,
            transition="grant->archived",
            reason=reason,
        )
