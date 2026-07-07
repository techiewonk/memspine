"""Prospective memory store (M13.8/ADR-016): watches as ordinary records.

A watch's ``content`` is what to do/remember when it fires. It reuses
existing columns — no new DDL: a *due* watch carries its due time in
``valid_from`` (a record whose relevance starts in the future); a *target*
watch carries the watched fact key in ``entity``/``attribute``.

Writes go through the engine door (``Engine.watch`` routes
:func:`make_watch_record` through the firewall gate like every other write);
this store owns the read side (``pending``) and the acknowledge transition —
a *delta* DECAY_TRANSITION through the injected write door, never a snapshot
(P3.1 rule), touching only projector-allow-listed fields.

Delivery is pull-based in v0.1 (ADR-016): ``pending()`` computes the fired
set on demand; nothing pushes. In ``event_log.mode: ephemeral`` no CONFLICT
events are readable, so invalidation watches degrade to never-firing (loud in
the ADR); due-time watches are unaffected.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import ClassVar, Protocol

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.exceptions import ConflictError
from memspine.memories.base import BaseMemory
from memspine.memories.prospective.triggers import fired_watches
from memspine.observability.logging import EVENT_DECAY_TRANSITION, get_logger

__all__ = ["ProspectiveMemory", "make_watch_record"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


class ProspectiveStore(Protocol):
    """The storage slice prospective needs (ports & adapters, D-22).

    ``read_events`` feeds invalidation detection: M4 CONFLICT events are the
    invalidation signal, and this store reads them from the log rather than
    keeping a second bookkeeping table (D0.1).
    """

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...


def make_watch_record(
    namespace: str,
    content: str,
    *,
    due_at: datetime | None = None,
    entity: str | None = None,
    attribute: str | None = None,
    source: SourceInfo | None = None,
) -> MemoryRecord:
    """A new watch. Exactly ONE trigger: a due time OR a watched fact key.

    Both-or-neither is refused loudly — a watch with no trigger would never
    fire (silent data loss), and a dual-trigger watch is two watches (v0.1
    keeps the record unambiguous; ADR-016).
    """
    if attribute is not None and entity is None:
        raise ConflictError("a watched attribute needs its entity")
    if due_at is None and entity is None:
        raise ConflictError(
            "a watch needs a trigger: either due_at or a watched entity[, attribute]"
        )
    if due_at is not None and entity is not None:
        raise ConflictError(
            "a watch takes ONE trigger — due_at or a watched target, not both "
            "(create two watches if you need both)"
        )
    if due_at is not None and due_at.tzinfo is None:
        raise ConflictError("due_at must be timezone-aware (naive datetimes are ambiguous)")
    return MemoryRecord(
        namespace=namespace,
        memory_type="prospective",
        content=content,
        entity=entity,
        attribute=attribute,
        # Bi-temporal reuse (ADR-016): the due time IS the record's
        # valid_from — "becomes relevant at". Target watches are relevant now.
        valid_from=due_at if due_at is not None else datetime.now(UTC),
        source=source or SourceInfo(role="user"),
    )


class ProspectiveMemory(BaseMemory):
    name: ClassVar[str] = "prospective"
    needs: ClassVar[tuple[str, ...]] = ("semantic",)

    def __init__(self, storage: ProspectiveStore, append_event: AppendEvent) -> None:
        self._storage = storage
        self._append_event = append_event

    async def pending(self, namespace: str, now: datetime) -> list[MemoryRecord]:
        """Fired-but-unacknowledged watches in ``namespace`` at ``now``.

        Read-only and idempotent: a fired watch stays pending until
        :meth:`acknowledge` archives it. ``now`` is explicit — callers own
        time (tests freeze it; no clock snooping inside).
        """
        watches = await self._storage.list_records(namespace, "prospective")
        has_target_watches = any(
            watch.entity is not None
            and watch.status is RecordStatus.ACTIVATED
            and not watch.quarantined
            for watch in watches
        )
        conflicts = await self._conflict_events(namespace) if has_target_watches else []
        return fired_watches(watches, now, conflicts)

    async def acknowledge(self, record_id: str, *, namespace: str) -> MemoryRecord:
        """Acknowledge a fired watch: ARCHIVED via a delta event through the
        door (``reason="watch_fired"``). Idempotent — acknowledging an already
        archived watch appends nothing and returns it unchanged."""
        record = await self._require_watch(record_id, namespace)
        # CMP-1/ADR-018: a quarantined watch is firewall-held (E1) — it can
        # never fire (pending() excludes it), so there is nothing to acknowledge.
        # Refuse rather than let this non-firewall path archive suspect content
        # (matches ProceduralMemory.promote's E1 stance).
        if record.quarantined:
            raise ConflictError(
                f"record {record_id} is quarantined (E1) — a held watch cannot fire, "
                "so there is nothing to acknowledge"
            )
        if record.status is RecordStatus.ARCHIVED:
            return record  # idempotent: already acknowledged
        now = datetime.now(UTC)
        change: dict[str, object] = {
            "status": RecordStatus.ARCHIVED.value,
            "valid_to": now.isoformat(),
        }
        await self._append_event(
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace=namespace,
                actor="system",
                payload={
                    "record_id": record.record_id,
                    "set": change,
                    "transition": "watch->acknowledged",
                    "reason": "watch_fired",
                },
            )
        )
        _log.info(
            EVENT_DECAY_TRANSITION,
            namespace=namespace,
            record_id=record.record_id,
            transition="watch->acknowledged",
        )
        return record.model_copy(update={"status": RecordStatus.ARCHIVED, "valid_to": now})

    async def _conflict_events(self, namespace: str) -> list[MemoryEvent]:
        """Every CONFLICT event in ``namespace`` — a batched log scan (D0.1:
        the log is the invalidation source of truth; ephemeral mode reads
        nothing, so invalidation watches simply never fire there)."""
        conflicts: list[MemoryEvent] = []
        after = 0
        while True:
            batch = await self._storage.read_events(after_seq=after)
            if not batch:
                return conflicts
            conflicts.extend(
                event
                for event in batch
                if event.kind is EventKind.CONFLICT and event.namespace == namespace
            )
            last_seq = batch[-1].seq
            assert last_seq is not None  # events past the door always carry seq
            after = last_seq

    async def _require_watch(self, record_id: str, namespace: str) -> MemoryRecord:
        record = await self._storage.get_record(record_id)
        # Namespace isolation: one error for missing AND foreign records — a
        # leaked record_id must not become a cross-namespace existence oracle
        # (ADR-014 shape).
        if record is None or record.namespace != namespace:
            raise ConflictError(f"no such record {record_id!r} in namespace {namespace!r}")
        if record.memory_type != "prospective":
            raise ConflictError(
                f"record {record_id} is {record.memory_type!r}, not prospective — "
                "only watches can be acknowledged (M13.8)"
            )
        return record
