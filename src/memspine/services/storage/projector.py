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
  demoted to dormant). Only current delta payloads are supported,
- ``FORGET`` — soft-delete (status=DELETED, valid_to closed); the M7 hard-delete
  cascade with referential retention lands in Phase 4,
- ``RETRIEVE`` — reinforcement stats (M1): ``last_accessed_at`` advances
  (max(), idempotent) and ``access_count`` increments. The counter is
  at-least-once — a crash between apply and checkpoint may recount a batch —
  which is acceptable by design for an approximate reinforcement signal.
- ``FEEDBACK`` is audit-only for public feedback; internal outcome receipts update procedural counters.
- ``SESSION`` (#53) — ``scoring.passive`` set (state ``passive``) or cleared
  (``active``) on every record the event lists. Idempotent.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

import orjson

from memspine.config import constants
from memspine.schemas.durable.semantic import SemanticEnvelope
from memspine.services.storage.records_port import RecordsProjectionPort, RecordsProjectionView
from memspine.store.claim_cardinality import group_key, record_group_of, value_token
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.projections.projector import Projector
from memspine.store.record_event_ids import affected_record_ids
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.store.source_binding import source_binding

__all__ = ["RecordProjector", "RecordStore", "fold_records"]

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


class RecordStore(RecordsProjectionPort, Protocol):
    """The slice of the storage port this projector needs — any backend qualifies."""

    async def upsert_record(self, record: MemoryRecord) -> None: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def delete_record(self, record_id: str) -> None: ...

    async def delete_all_records(self) -> None: ...


class _RecordReducer:
    def __init__(self, store: RecordsProjectionView) -> None:
        self._store = store

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            record = MemoryRecord.model_validate(event.payload["record"])
            if record.namespace != event.namespace:
                raise ValueError("WRITE record namespace differs from canonical event namespace")
            erased, _hidden = await self._store.erasure_state(event.namespace, record.record_id)
            if erased:
                # A pre-fence WRITE in a replaying log, or an invalid already
                # committed rewrite, cannot rehydrate a hard-erased identity.
                # Fresh appends are rejected by the native append fence.
                await self._store.delete_record(record.record_id)
                return
            # The event author is never the authority for a derived current conflict.
            record = _with_cardinality_flag(record, False)
            # DS-11 known-time horizon: this is the canonical acceptance sequence, derived
            # from the event envelope on every replay. Preserve the prior snapshot's sequence
            # for metadata-only rewrites; a content/version/source-binding change is new.
            previous = await self._store.get_record(record.record_id)
            if (
                previous is not None
                and previous.version == record.version
                and previous.content == record.content
                and source_binding(previous).binding_hash == source_binding(record).binding_hash
            ):
                source_seq = previous.fingerprint.source_seq
            else:
                source_seq = event.seq
            record = record.model_copy(
                update={
                    "fingerprint": record.fingerprint.model_copy(update={"source_seq": source_seq})
                }
            )
            await self._store.put_record(record)
        elif event.kind is EventKind.DECAY_TRANSITION:
            await self._apply_decay_transition(event)
        elif event.kind is EventKind.FORGET:
            await self._apply_forget(event)
        elif event.kind is EventKind.RETRIEVE:
            await self._apply_retrieve(event)
        elif event.kind is EventKind.FEEDBACK:
            await self._apply_outcome_receipt(event)
        elif event.kind is EventKind.SESSION:
            await self._apply_session(event)
        elif event.kind is EventKind.MARKER:
            await self._apply_erasure_marker(event)
        # other kinds materialize in later phases (M2-M7)

    async def _apply_decay_transition(self, event: MemoryEvent) -> None:
        payload = event.payload
        record = await self._store.get_record(str(payload["record_id"]))
        if record is None:
            # A FORGET/hard-delete raced the transition: nothing to patch.
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
            for key in unknown:
                delta.pop(key)
        data = record.model_dump(mode="json")
        data.update(delta)
        if added:
            new = [t for t in dict.fromkeys(added) if t not in record.tags]
            data["tags"] = [*record.tags, *new]
        # JSON round-trip so base64-encoded bytes fields (content_zstd)
        # validate the same way they serialize (D-38).
        await self._store.put_record(MemoryRecord.model_validate_json(orjson.dumps(data)))

    async def _apply_forget(self, event: MemoryEvent) -> None:
        record_id = str(event.payload["record_id"])
        if event.payload.get("hard"):
            # M7 hard-delete cascade: the row leaves the read model entirely
            # (idempotent — deleting an absent row is a no-op). The log side
            # (payload redaction) is the storage service's job, engine-driven.
            await self._store.mark_erased(event.namespace, record_id)
            await self._store.delete_record(record_id)
            return
        record = await self._store.get_record(record_id)
        if record is None or record.status is RecordStatus.DELETED:
            return  # idempotent: already gone
        await self._store.put_record(
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
                    #
                    # E1 interaction: the ceiling is the record's own trust, not a
                    # global constant. Without it, reinforcement was a ranking-level
                    # privilege escalation — search emits RETRIEVE over EVERY scored
                    # record, utility has no decrement site anywhere, and the composite
                    # score adds 0.5*utility against a relevance term whose whole range
                    # is 1/3. So ~10 searches let any ACTIVATED record outrank a
                    # perfectly relevant one at any relevance. External channels are
                    # capped at TRUST_RETRIEVED_CAP (0.3) but that is above
                    # QUARANTINE_TRUST_THRESHOLD (0.25), so untrusted content was
                    # admitted and fully pumpable. Bounding by trust makes the
                    # firewall's graded verdict load-bearing at the read door, where it
                    # previously collapsed to a binary. Replay-safe: trust is immutable
                    # post-write (see _DELTA_MUTABLE). See tests/unit/
                    # test_reinforcement_trust_cap.py.
                    "utility": min(
                        record.scoring.utility + constants.RETRIEVE_UTILITY_STEP,
                        constants.RETRIEVE_UTILITY_MAX,
                        record.trust,
                    ),
                }
            )
            await self._store.put_record(record.model_copy(update={"scoring": scoring}))

    async def _apply_outcome_receipt(self, event: MemoryEvent) -> None:
        """Materialize current internal outcome receipts; public feedback is audit-only."""
        receipt = event.payload.get("feedback")
        if (
            event.actor != "system"
            or not isinstance(receipt, dict)
            or receipt.get("channel") != "outcome_receipt"
        ):
            return
        field = {
            "confirmation": "likes",
            "rejection": "dislikes",
            "annotation": "notes",
        }.get(str(receipt.get("kind")))
        if field is None:
            return
        record = await self._store.get_record(str(event.payload.get("record_id")))
        if record is None:
            return
        scoring = record.scoring.model_copy(update={field: getattr(record.scoring, field) + 1})
        await self._store.put_record(record.model_copy(update={"scoring": scoring}))

    async def _apply_session(self, event: MemoryEvent) -> None:
        passive = event.payload.get("state") == "passive"
        for raw_id in event.payload.get("record_ids", []):
            record = await self._store.get_record(str(raw_id))
            if record is None or record.scoring.passive is passive:
                continue
            scoring = record.scoring.model_copy(update={"passive": passive})
            await self._store.put_record(record.model_copy(update={"scoring": scoring}))

    async def _apply_erasure_marker(self, event: MemoryEvent) -> None:
        payload = event.payload
        marker = payload.get("marker")
        if marker == "erasure_manifest":
            manifest = payload.get("manifest") or {}
            ids = manifest.get("ids") or {}
            for raw_id in ids.get("record_ids") or []:
                rid = str(raw_id)
                await self._store.mark_erased(event.namespace, rid)
                await self._store.delete_record(rid)
            for raw_id in payload.get("hidden") or []:
                await self._store.mark_hidden(event.namespace, str(raw_id))
        elif marker == "erasure_release":
            for raw_id in payload.get("record_ids") or []:
                await self._store.release_hidden(event.namespace, str(raw_id))


def _with_cardinality_flag(record: MemoryRecord, flag: bool) -> MemoryRecord:
    raw = record.fingerprint.envelope
    if raw is None or raw.get("cardinality_conflict") is flag:
        return record
    fingerprint = record.fingerprint.model_copy(
        update={"envelope": {**raw, "cardinality_conflict": flag}}
    )
    return record.model_copy(update={"fingerprint": fingerprint})


def _key_of(record: MemoryRecord | None) -> str | None:
    group = record_group_of(record) if record is not None else None
    return group_key(group) if group is not None else None


async def _group_page_all(
    view: RecordsProjectionView, namespace: str, key: str, limit: int = 256
) -> list[MemoryRecord]:
    """Read a complete indexed group; never infer absence from a truncated page."""
    rows: list[MemoryRecord] = []
    after: str | None = None
    while True:
        page = await view.group_page(namespace, key, after_id=after, limit=limit)
        if not page:
            return rows
        rows.extend(page)
        after = page[-1].record_id
        if len(page) < limit:
            return rows


async def _recompute_group(view: RecordsProjectionView, namespace: str, key: str) -> None:
    rows = await _group_page_all(view, namespace, key)
    participants: list[MemoryRecord] = []
    values: set[tuple[object, ...]] = set()
    for row in rows:
        if _key_of(row) != key:
            continue  # defense against a stale or colliding physical index entry
        erased, hidden = await view.erasure_state(namespace, row.record_id)
        if erased or hidden:
            continue
        raw = row.fingerprint.envelope
        if raw is None:
            continue
        token = value_token(SemanticEnvelope.model_validate(raw))
        if token is None:
            continue
        participants.append(row)
        values.add(token)
    flagged = len(values) > 1
    participant_ids = {row.record_id for row in participants}
    for row in rows:
        target = flagged and row.record_id in participant_ids
        updated = _with_cardinality_flag(row, target)
        if updated is not row:
            await view.put_record(updated)


async def fold_records(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
    """Fold an ordered canonical page and derive exact current group flags.

    This function is retry-pure: all reads and writes use the supplied native
    transaction view; it never appends events, changes process state or emits an
    external effect. The caller commits rows, erasure membership and cursor as
    one unit only after this returns.
    """
    reducer = _RecordReducer(view)
    dirty: set[tuple[str, str]] = set()
    for event in events:
        ids = affected_record_ids(event)
        for rid in ids:
            previous = await view.get_record(rid)
            if previous is not None and previous.namespace != event.namespace:
                raise ValueError("record event targets a record in another namespace")
            key = _key_of(previous)
            if key is not None:
                dirty.add((event.namespace, key))
        await reducer.apply(event)
        for rid in ids:
            current = await view.get_record(rid)
            key = _key_of(current)
            if key is not None:
                dirty.add((event.namespace, key))
            elif current is not None:
                # The physical group index no longer returns a departed row.
                # Clear its own derived flag before recomputing the survivors.
                cleared = _with_cardinality_flag(current, False)
                if cleared is not current:
                    await view.put_record(cleared)
    for namespace, key in sorted(dirty):
        await _recompute_group(view, namespace, key)


class RecordProjector(Projector):
    name = "records"

    def __init__(self, store: RecordStore) -> None:
        self._store = store

    async def apply(self, event: MemoryEvent) -> None:
        if event.seq is None:
            raise ValueError("records projection needs a sequenced canonical event")
        while True:
            receipt = await self._store.advance_records(event.seq, 256, fold_records)
            if receipt.complete:
                return
            if receipt.applied_count == 0:
                raise RuntimeError(f"record event seq {event.seq} is not available to project")

    async def reset(self) -> None:
        await self._store.reset_records_projection()
