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
- ``FEEDBACK`` (#54) — per-record feedback counts in ``scoring``: a ``like`` or
  ``dislike`` increments its count; any event carrying a note increments ``notes``. At-least-once like RETRIEVE (a crash between
  apply and checkpoint may recount); the transform that reads them is bounded.
- ``SESSION`` (#53) — ``scoring.passive`` set (state ``passive``) or cleared
  (``active``) on every record the event lists. Idempotent.
"""

from __future__ import annotations

from typing import Protocol

import orjson

from memspine.config import constants
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.observability.logging import get_logger

__all__ = ["RecordProjector", "RecordStore"]

_log = get_logger(__name__)

#: Fields a DECAY_TRANSITION delta may patch — the lifecycle surface. Notably
#: ABSENT: ``trust`` (only the firewall assigns it) and any identity/content
#: field except the cold-tier compress pair (content -> content_zstd, M6).
_DELTA_MUTABLE = frozenset(
    {
        "status",
        "superseded_at",
        "evolve_to",
        "tier",
        "content",
        "content_zstd",
        "valid_to",
        "quarantined",
        "corroborations",
        "skill_stage",
        "memory_type",  # working -> episodic page-out (M13.1)
        "version",
    }
)

#: Add-only tag keys (B-8). A delta can never replace or drop a record's tags —
#: the N3 ``taint_archived`` mark must survive any later lifecycle patch — so
#: tags only grow, as a union. ``tags_add`` is what the engine emits; ``tags``
#: is what logs written before B-8 carry, and replays with the same union
#: (those deltas only ever appended the mark, so the outcome is unchanged).
_DELTA_TAG_UNION = frozenset({"tags_add", "tags"})


#: #54: the feedback signals the projector counts.
_FEEDBACK_SIGNALS = frozenset({"like", "dislike", "note"})


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
        elif event.kind is EventKind.FEEDBACK:
            await self._apply_feedback(event)
        elif event.kind is EventKind.SESSION:
            await self._apply_session(event)
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
        delta = dict(payload.get("set") or {})
        added: list[str] = []
        for key in _DELTA_TAG_UNION & set(delta):
            value = delta.pop(key)
            if isinstance(value, list):
                added.extend(str(tag) for tag in value)
        # Allow-list gate (E1): a delta is a lifecycle patch, not a general
        # writer — fields like trust/content that only the firewall or a WRITE
        # may set are dropped loudly instead of silently applied.
        unknown = set(delta) - _DELTA_MUTABLE
        if unknown:
            _log.warning(
                "decay_transition.illegal_delta_keys_dropped",
                record_id=payload["record_id"],
                keys=sorted(unknown),
                reason=payload.get("reason"),
            )
            for key in unknown:
                delta.pop(key)
        data = record.model_dump(mode="json")
        data.update(delta)
        if added:
            new = [t for t in dict.fromkeys(added) if t not in record.tags]
            data["tags"] = [*record.tags, *new]
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
                    # A5 reinforcement-on-read: durable salience lift, clamped so
                    # a hot record can't run away. Rides the RETRIEVE event, so a
                    # rebuild replays it deterministically.
                    "utility": min(
                        record.scoring.utility + constants.RETRIEVE_UTILITY_STEP,
                        constants.RETRIEVE_UTILITY_MAX,
                    ),
                }
            )
            await self._store.upsert_record(record.model_copy(update={"scoring": scoring}))

    async def _apply_feedback(self, event: MemoryEvent) -> None:
        record = await self._store.get_record(str(event.payload.get("record_id")))
        signal = str(event.payload.get("signal"))
        if record is None or signal not in _FEEDBACK_SIGNALS:
            return
        update: dict[str, int] = {}
        if signal == "like":
            update["likes"] = record.scoring.likes + 1
        elif signal == "dislike":
            update["dislikes"] = record.scoring.dislikes + 1
        if signal == "note" or event.payload.get("content"):
            update["notes"] = record.scoring.notes + 1  # every event carrying a note
        scoring = record.scoring.model_copy(update=update)
        await self._store.upsert_record(record.model_copy(update={"scoring": scoring}))

    async def _apply_session(self, event: MemoryEvent) -> None:
        passive = event.payload.get("state") == "passive"
        for raw_id in event.payload.get("record_ids", []):
            record = await self._store.get_record(str(raw_id))
            if record is None or record.scoring.passive is passive:
                continue
            scoring = record.scoring.model_copy(update={"passive": passive})
            await self._store.upsert_record(record.model_copy(update={"scoring": scoring}))

    async def reset(self) -> None:
        await self._store.delete_all_records()
