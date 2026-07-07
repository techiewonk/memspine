"""Relational record projector: the storage read model as a projection (D0.1).

Even in Phase 0 the write path is honest event sourcing: ``Engine.write``
appends to the log, and *this projector* — not the write call — materializes
``memory_records``. Rebuild drops the read model and replays.

Handled kinds (M11):

- ``WRITE`` — upsert the carried full record snapshot,
- ``DECAY_TRANSITION`` — a *delta*: ``{record_id, set}`` merged onto the current
  row. Lifecycle transitions must never carry full snapshots — a snapshot taken
  before the append overwrites whatever changed in between (empirically: a
  concurrent RETRIEVE's access stats were erased and a just-accessed record
  demoted to dormant). Legacy full-snapshot payloads (``record`` key) from
  pre-P3.1 logs still replay,
- ``FORGET`` — soft-delete (status=DELETED, valid_to closed); the M7 hard-delete
  cascade with referential retention lands in Phase 4,
- ``RETRIEVE`` — reinforcement stats (M1): ``last_accessed_at`` advances
  (max(), idempotent) and ``access_count`` increments. The counter is
  at-least-once — a crash between apply and checkpoint may recount a batch —
  which is acceptable by design for an approximate reinforcement signal.
"""

from __future__ import annotations

from typing import Protocol

import orjson

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.observability.logging import get_logger

__all__ = ["RecordProjector", "RecordStore"]

_log = get_logger(__name__)


class RecordStore(Protocol):
    """The slice of the storage port this projector needs — any backend qualifies."""

    async def upsert_record(self, record: MemoryRecord) -> None: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def delete_record(self, record_id: str) -> None: ...

    async def delete_all_records(self) -> None: ...


class RecordProjector(Projector):
    name = "records"

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            record = MemoryRecord.model_validate(event.payload["record"])
            await self._store.upsert_record(record)  # upsert => idempotent replay
        elif event.kind is EventKind.DECAY_TRANSITION:
            await self._apply_decay_transition(event)
        elif event.kind is EventKind.FORGET:
            await self._apply_forget(event)
        elif event.kind is EventKind.RETRIEVE:
            await self._apply_retrieve(event)
        # other kinds materialize in later phases (M2-M7)

    async def _apply_decay_transition(self, event: MemoryEvent) -> None:
        payload = event.payload
        if "record" in payload:  # legacy full snapshot (pre-P3.1 logs) — replayable
            await self._store.upsert_record(MemoryRecord.model_validate(payload["record"]))
            return
        record = await self._store.get_record(str(payload["record_id"]))
        if record is None:
            # A FORGET/hard-delete raced the transition: nothing to patch.
            _log.info(
                "decay_transition.orphaned",
                record_id=payload["record_id"],
                reason=payload.get("reason"),
            )
            return
        data = record.model_dump(mode="json")
        data.update(payload.get("set") or {})
        # JSON round-trip so base64-encoded bytes fields (content_zstd)
        # validate the same way they serialize (D-38).
        await self._store.upsert_record(MemoryRecord.model_validate_json(orjson.dumps(data)))

    async def _apply_forget(self, event: MemoryEvent) -> None:
        record_id = str(event.payload["record_id"])
        if event.payload.get("hard"):
            # M7 hard-delete cascade: the row leaves the read model entirely
            # (idempotent — deleting an absent row is a no-op). The log side
            # (payload redaction) is the storage service's job, engine-driven.
            await self._store.delete_record(record_id)
            return
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
