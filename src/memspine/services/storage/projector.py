"""Relational record projector: the storage read model as a projection (D0.1).

Even in Phase 0 the write path is honest event sourcing: ``Engine.write``
appends to the log, and *this projector* — not the write call — materializes
``memory_records``. Rebuild drops the read model and replays.

Handled kinds (M11):

- ``WRITE`` / ``DECAY_TRANSITION`` — upsert the carried record snapshot,
- ``FORGET`` — soft-delete (status=DELETED, valid_to closed); the M7 hard-delete
  cascade with referential retention lands in Phase 4,
- ``RETRIEVE`` — reinforcement stats (M1): ``last_accessed_at`` advances
  (max(), idempotent) and ``access_count`` increments. The counter is
  at-least-once — a crash between apply and checkpoint may recount a batch —
  which is acceptable by design for an approximate reinforcement signal.
"""

from __future__ import annotations

from typing import Protocol

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord, RecordStatus

__all__ = ["RecordProjector", "RecordStore"]


class RecordStore(Protocol):
    """The slice of the storage port this projector needs — any backend qualifies."""

    async def upsert_record(self, record: MemoryRecord) -> None: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def delete_all_records(self) -> None: ...


class RecordProjector(Projector):
    name = "records"

    #: Kinds that carry a full record snapshot to materialize.
    _MATERIALIZING = frozenset({EventKind.WRITE, EventKind.DECAY_TRANSITION})

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind in self._MATERIALIZING:
            record = MemoryRecord.model_validate(event.payload["record"])
            await self._store.upsert_record(record)  # upsert => idempotent replay
        elif event.kind is EventKind.FORGET:
            await self._apply_forget(event)
        elif event.kind is EventKind.RETRIEVE:
            await self._apply_retrieve(event)
        # other kinds materialize in later phases (M2-M7)

    async def _apply_forget(self, event: MemoryEvent) -> None:
        record_id = str(event.payload["record_id"])
        record = await self._store.get_record(record_id)
        if record is None or record.status is RecordStatus.DELETED:
            return  # idempotent: already gone
        await self._store.upsert_record(
            record.model_copy(
                update={
                    "status": RecordStatus.DELETED,
                    "valid_to": event.ts,
                    "superseded_at": event.ts,
                }
            )
        )

    async def _apply_retrieve(self, event: MemoryEvent) -> None:
        for raw_id in event.payload.get("record_ids", []):
            record = await self._store.get_record(str(raw_id))
            if record is None:
                continue
            scoring = record.scoring.model_copy(
                update={
                    "last_accessed_at": (
                        max(record.scoring.last_accessed_at, event.ts)
                        if record.scoring.last_accessed_at
                        else event.ts
                    ),
                    "access_count": record.scoring.access_count + 1,
                }
            )
            await self._store.upsert_record(record.model_copy(update={"scoring": scoring}))

    async def reset(self) -> None:
        await self._store.delete_all_records()
