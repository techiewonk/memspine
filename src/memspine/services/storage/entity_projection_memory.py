"""In-process reference for the ordered entity-index projection port.

Used by model-free tests; SQLite and MongoDB provide the durable implementations.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from memspine.exceptions import StorageError
from memspine.services.storage.entity_index_tables import (
    EntityIndexRow,
    MemoryEntityIndexStore,
)
from memspine.services.storage.records_port import (
    EntityIndexAdvance,
    EntityIndexApplyBatch,
    EntityIndexApplyPurge,
    EntityIndexProjectionView,
)
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

_KINDS = frozenset(
    {
        EventKind.WRITE,
        EventKind.FORGET,
        EventKind.MARKER,
        EventKind.DECAY_TRANSITION,
        EventKind.REGISTRY_ERASURE_FENCE,
    }
)


class _View(EntityIndexProjectionView):
    def __init__(self, owner: MemoryEntityIndexProjection, namespace: str) -> None:
        self._owner = owner
        self._namespace = namespace

    def _check(self, namespace: str) -> None:
        if namespace != self._namespace:
            raise StorageError("entity-index view namespace mismatch")

    async def put(self, rows: Sequence[EntityIndexRow]) -> None:
        for row in rows:
            self._check(row.namespace)
        await self._owner.index.put(rows)

    async def select(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
        match: str | None = None,
    ) -> list[EntityIndexRow]:
        self._check(namespace)
        return await self._owner.index.select(
            namespace, kind, key=key, entity_id=entity_id, ref=ref, match=match
        )

    async def delete(
        self,
        namespace: str,
        kind: str,
        *,
        key: str | None = None,
        entity_id: str | None = None,
        ref: str | None = None,
    ) -> None:
        self._check(namespace)
        await self._owner.index.delete(namespace, kind, key=key, entity_id=entity_id, ref=ref)

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        record = self._owner.records.get(record_id)
        return record if record is not None and record.namespace == self._namespace else None

    async def pending_events(self, limit: int) -> list[MemoryEvent]:
        rows = await self._owner.index.select(self._namespace, "pending_positive")
        events: list[MemoryEvent] = []
        for row in sorted(rows, key=lambda item: item.seq)[:limit]:
            seq = row.seq
            if seq < 1 or seq > len(self._owner.events):
                raise StorageError("pending entity-index canonical event is missing")
            event = self._owner.events[seq - 1]
            if (
                event.seq != seq
                or event.namespace != self._namespace
                or event.kind is not EventKind.MARKER
            ):
                raise StorageError("pending entity-index canonical event mismatches")
            events.append(event)
        return events


class MemoryEntityIndexProjection:
    """One shared in-memory log/index with atomic cursor and retry-pure callbacks."""

    def __init__(self) -> None:
        self.events: list[MemoryEvent] = []
        self.records: dict[str, MemoryRecord] = {}
        self.index = MemoryEntityIndexStore()
        self.marks: dict[str, int] = {"entity_index": 0}
        self._cursor: dict[str, int] = {}
        self._scan_cursor: dict[str, int] = {}
        self._revision: dict[str, int] = {}
        self._epoch = 0
        self._lock = asyncio.Lock()

    def entity_index_store(self) -> MemoryEntityIndexStore:
        return self.index

    async def initialize_entity_index(self) -> None:
        self.marks.setdefault("entity_index", 0)

    def append(self, event: MemoryEvent) -> MemoryEvent:
        appended = event.model_copy(update={"seq": len(self.events) + 1})
        self.events.append(appended)
        if event.kind is EventKind.WRITE and isinstance(event.payload.get("record"), dict):
            record = MemoryRecord.model_validate(event.payload["record"])
            self.records[record.record_id] = record
        elif event.kind is EventKind.FORGET:
            record_id = event.payload.get("record_id")
            if isinstance(record_id, str):
                self.records.pop(record_id, None)
        return appended

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        return [event for event in self.events if (event.seq or 0) > after_seq][:limit]

    async def read_scoped_events(
        self, namespace: str, kinds: Sequence[EventKind], *, after_seq: int, limit: int
    ) -> tuple[list[MemoryEvent], int]:
        head = len(self.events)
        return [
            event
            for event in self.events
            if after_seq < (event.seq or 0) <= head
            and event.namespace == namespace
            and event.kind in kinds
        ][:limit], head

    async def get_offset(self, name: str) -> int:
        return self.marks.get(name, 0)

    async def set_offset(self, name: str, seq: int) -> None:
        if name == "entity_index":
            raise ValueError("entity index owns its checkpoint")
        self.marks[name] = max(self.marks.get(name, 0), seq)

    def _global_prefix(self, limit: int) -> int:
        current = self.marks["entity_index"]
        for event in self.events[current : current + limit]:
            assert event.seq is not None
            if event.kind in _KINDS and self._cursor.get(event.namespace, 0) < event.seq:
                break
            current = event.seq
        self.marks["entity_index"] = current
        return current

    async def advance_entity_index(
        self,
        namespace: str,
        target_seq: int,
        limit: int,
        apply_batch: EntityIndexApplyBatch,
    ) -> EntityIndexAdvance:
        async with self._lock:
            scan_cursor = self._scan_cursor.get(namespace, 0)
            scoped, head = await self.read_scoped_events(
                namespace, tuple(_KINDS), after_seq=scan_cursor, limit=limit + 1
            )
            target = max(scan_cursor, min(target_seq, head))
            pending = [event for event in scoped if (event.seq or 0) <= target]
            batch = pending[:limit]
            previous = dict(self.index._rows)
            try:
                effects = await apply_batch(_View(self, namespace), batch)
            except BaseException:
                self.index._rows = previous
                raise
            page_complete = len(pending) <= limit
            scan_through = target if page_complete else int(batch[-1].seq or scan_cursor)
            self._scan_cursor[namespace] = scan_through
            pending_rows = await self.index.select(namespace, "pending_positive")
            earliest = min((row.seq for row in pending_rows), default=None)
            through = min(scan_through, earliest - 1) if earliest is not None else scan_through
            self._cursor[namespace] = through
            complete = page_complete and through >= target
            if effects.changed:
                self._revision[namespace] = self._revision.get(namespace, 0) + 1
            global_offset = self._global_prefix(limit)
            return EntityIndexAdvance(
                self._epoch,
                through,
                self._revision.get(namespace, 0),
                global_offset,
                complete,
                len(batch),
                effects,
            )

    async def purge_entity_index(
        self,
        namespace: str,
        record_ids: Sequence[str],
        apply_purge: EntityIndexApplyPurge,
    ) -> EntityIndexAdvance:
        async with self._lock:
            previous = dict(self.index._rows)
            try:
                effects = await apply_purge(_View(self, namespace), record_ids)
            except BaseException:
                self.index._rows = previous
                raise
            if effects.changed:
                self._revision[namespace] = self._revision.get(namespace, 0) + 1
            pending_rows = await self.index.select(namespace, "pending_positive")
            earliest = min((row.seq for row in pending_rows), default=None)
            scan = self._scan_cursor.get(namespace, 0)
            certified = min(scan, earliest - 1) if earliest is not None else scan
            self._cursor[namespace] = certified
            global_offset = self._global_prefix(2048)
            return EntityIndexAdvance(
                self._epoch,
                certified,
                self._revision.get(namespace, 0),
                global_offset,
                True,
                0,
                effects,
            )

    async def reset_entity_index(self) -> EntityIndexAdvance:
        async with self._lock:
            await self.index.clear()
            self._cursor.clear()
            self._scan_cursor.clear()
            self._revision.clear()
            self._epoch += 1
            self.marks["entity_index"] = 0
            return EntityIndexAdvance(self._epoch, 0, 0, 0, True, 0)

    async def entity_index_revision(self, namespace: str) -> tuple[int, int]:
        return self._epoch, self._revision.get(namespace, 0)
