"""Relational record projector: the storage read model as a projection (D0.1).

Even in Phase 0 the write path is honest event sourcing: ``Engine.write``
appends to the log, and *this projector* — not the write call — materializes
``memory_records``. Rebuild drops the read model and replays.
"""

from __future__ import annotations

from typing import Protocol

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord

__all__ = ["RecordProjector", "RecordStore"]


class RecordStore(Protocol):
    """The slice of the storage port this projector needs — any backend qualifies."""

    async def upsert_record(self, record: MemoryRecord) -> None: ...

    async def delete_all_records(self) -> None: ...


class RecordProjector(Projector):
    name = "records"

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is not EventKind.WRITE:
            return  # other kinds materialize in later phases (M2-M7)
        record = MemoryRecord.model_validate(event.payload["record"])
        await self._store.upsert_record(record)  # upsert => idempotent replay

    async def reset(self) -> None:
        await self._store.delete_all_records()
