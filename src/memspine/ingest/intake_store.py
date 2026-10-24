"""Durable ledger for the intake gateway (W2-01; LIB-30 ``IntakeStore`` over SQL).

``IntakeBook`` (``ingest.intake``) is pure and synchronous; its state lives in an injected store. This
module persists that state in the engine's own database. Its tables are part of the current
fresh SQL baseline:

``intake_source``  one row per (scope, source): latest version, version records, live segments (JSON).
``intake_write``   one row per S3-3 write id, with the real record id once the record exists.
``intake_intent``  one row per accepted envelope: receipt, readiness, the apply payload while the
                   records are still being written (``status`` ``accepted`` -> ``applied``).

A call is *committed* when :func:`commit` returns: the source state, the new write ids, the receipt
and the apply payload are written in ONE transaction, so a crash leaves either nothing or all of it.
The apply payload is what lets the next ``start()`` finish an interrupted write; it is cleared (set to
NULL) as soon as the records exist, so no source text stays in the ledger.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.ingest.intake import (
    InMemoryIntakeStore,
    IntakeChange,
    LiveSegment,
    SourceState,
    VersionRecord,
)
from memspine.schemas.public.receipt import Readiness, Receipt
from memspine.services.storage.intake_tables import (
    INTAKE_TABLES as INTAKE_TABLES,
)
from memspine.services.storage.intake_tables import (
    conversation_identity,
    intake_intent,
    intake_source,
    intake_write,
    message_identity,
)
from memspine.services.storage.intake_tables import (
    intake_metadata as intake_metadata,
)

__all__ = [
    "INTAKE_TABLES",
    "Overlay",
    "applied_intent",
    "commit",
    "ensure_schema",
    "intents_by_id",
    "load_overlay",
    "pending_intents",
    "record_ids",
    "set_readiness",
]


async def ensure_schema(engine: AsyncEngine) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(intake_metadata.create_all)


# -------------------------------------------------------------------------------------------- codecs
def _live_to_json(seg: LiveSegment) -> dict[str, Any]:
    return asdict(seg)


def _live_from_json(d: Mapping[str, Any]) -> LiveSegment:
    return LiveSegment(**d)


def _state_to_json(state: SourceState) -> str:
    return json.dumps(
        {
            "source_id": state.source_id,
            "kind": state.kind,
            "latest_version": state.latest_version,
            "versions": {str(k): asdict(v) for k, v in state.versions.items()},
            "live": [_live_to_json(s) for s in state.live],
        },
        sort_keys=True,
    )


def _state_from_json(text: str) -> SourceState:
    d = json.loads(text)
    return SourceState(
        source_id=d["source_id"],
        kind=d["kind"],
        latest_version=d["latest_version"],
        versions={int(k): VersionRecord(**v) for k, v in d["versions"].items()},
        live=tuple(_live_from_json(s) for s in d["live"]),
    )


# ------------------------------------------------------------------------------------------- overlay
class Overlay(InMemoryIntakeStore):
    """The slice of the ledger one accept needs, as a sync store; ``commit`` is captured, not saved."""

    def __init__(self) -> None:
        super().__init__()
        self.change: IntakeChange | None = None

    def commit(self, change: IntakeChange) -> None:
        self.change = change
        super().commit(change)


async def load_overlay(
    engine: AsyncEngine, skey: str, source_id: str, write_ids: Sequence[str]
) -> Overlay:
    """Source state, the named write ids and every receipt they point at, as a sync store."""
    ov = Overlay()
    intent_ids: set[str] = set()
    async with engine.connect() as conn:
        row = (
            await conn.execute(
                select(intake_source.c.state).where(
                    intake_source.c.scope_key == skey, intake_source.c.source_id == source_id
                )
            )
        ).first()
        if row is not None:
            state = _state_from_json(row.state)
            ov._sources[(skey, source_id)] = state
            intent_ids.update(v.intent_id for v in state.versions.values())
        if write_ids:
            rows = (
                await conn.execute(
                    select(intake_write.c.write_id, intake_write.c.data).where(
                        intake_write.c.scope_key == skey,
                        intake_write.c.write_id.in_(list(write_ids)),
                    )
                )
            ).all()
            for r in rows:
                live = _live_from_json(json.loads(r.data))
                ov._writes[(skey, r.write_id)] = live
                intent_ids.add(live.intent_id)
        if intent_ids:
            intent_rows = (
                await conn.execute(
                    select(
                        intake_intent.c.intent_id,
                        intake_intent.c.receipt,
                        intake_intent.c.readiness,
                    ).where(intake_intent.c.intent_id.in_(sorted(intent_ids)))
                )
            ).all()
            for intent_row in intent_rows:
                ov._receipts[intent_row.intent_id] = Receipt.model_validate_json(
                    intent_row.receipt
                ).model_copy(
                    update={"readiness": Readiness.model_validate_json(intent_row.readiness)}
                )
                ov._readiness[intent_row.intent_id] = Readiness.model_validate_json(
                    intent_row.readiness
                )
    return ov


async def intents_by_id(engine: AsyncEngine, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    wanted = sorted(set(ids))
    if not wanted:
        return out
    async with engine.connect() as conn:
        rows = (
            await conn.execute(select(intake_intent).where(intake_intent.c.intent_id.in_(wanted)))
        ).all()
    for r in rows:
        out[r.intent_id] = dict(r._mapping)
    return out


async def pending_intents(engine: AsyncEngine) -> list[dict[str, Any]]:
    """Accepted-but-not-applied intents, oldest first (a restart finishes them in order)."""
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(intake_intent)
                .where(intake_intent.c.status == "accepted")
                .order_by(intake_intent.c.created_at, intake_intent.c.intent_id)
            )
        ).all()
    return [dict(r._mapping) for r in rows]


async def record_ids(engine: AsyncEngine, skey: str, write_ids: Sequence[str]) -> dict[str, str]:
    if not write_ids:
        return {}
    async with engine.connect() as conn:
        rows = (
            await conn.execute(
                select(intake_write.c.write_id, intake_write.c.record_id).where(
                    intake_write.c.scope_key == skey,
                    intake_write.c.write_id.in_(list(write_ids)),
                    intake_write.c.record_id.is_not(None),
                )
            )
        ).all()
    return {r.write_id: r.record_id for r in rows}


# --------------------------------------------------------------------------------------------- writes
async def commit(
    engine: AsyncEngine,
    change: IntakeChange,
    *,
    namespace: str,
    apply_payload: str,
    created_at: str,
) -> None:
    """The one transaction of an accept: source state, new write ids, receipt, apply payload."""
    skey = change.scope_key
    state = SourceState(
        change.source_id,
        change.kind,
        change.version,
        {change.version: change.version_record},
        change.live,
    )
    try:
        async with engine.begin() as conn:
            row = (
                await conn.execute(
                    select(intake_source.c.state).where(
                        intake_source.c.scope_key == skey,
                        intake_source.c.source_id == change.source_id,
                    )
                )
            ).first()
            if row is None:
                await conn.execute(
                    insert(intake_source).values(
                        scope_key=skey, source_id=change.source_id, state=_state_to_json(state)
                    )
                )
            else:
                prev = _state_from_json(row.state)
                versions = {**prev.versions, change.version: change.version_record}
                merged = SourceState(
                    change.source_id,
                    change.kind,
                    max(prev.latest_version, change.version),
                    versions,
                    change.live,
                )
                await conn.execute(
                    update(intake_source)
                    .where(
                        intake_source.c.scope_key == skey,
                        intake_source.c.source_id == change.source_id,
                    )
                    .values(state=_state_to_json(merged))
                )
            fresh = set(change.new_write_ids)
            for seg in change.live:
                if seg.write_id in fresh:
                    await conn.execute(
                        insert(intake_write).values(
                            scope_key=skey,
                            write_id=seg.write_id,
                            source_id=change.source_id,
                            record_id=None,
                            data=json.dumps(_live_to_json(seg), sort_keys=True),
                        )
                    )
            await conn.execute(
                insert(intake_intent).values(
                    intent_id=change.receipt.intent_id,
                    scope_key=skey,
                    namespace=namespace,
                    source_id=change.source_id,
                    status="accepted",
                    receipt=change.receipt.model_dump_json(),
                    readiness=change.readiness.model_dump_json(),
                    apply=apply_payload,
                    created_at=created_at,
                )
            )
            payload = json.loads(apply_payload)
            if payload.get("adapter") == "messages":
                for item in payload.get("fresh", []):
                    turn = item.get("turn", {})
                    meta = turn.get("_message_envelope")
                    if not isinstance(meta, Mapping):
                        continue
                    mid, cid, sid = (
                        meta.get("message_id"),
                        meta.get("conversation_id"),
                        meta.get("session_id"),
                    )
                    if not all(isinstance(v, str) and v for v in (mid, cid, sid)):
                        raise ValueError(
                            "message identity requires message_id, conversation_id, and session_id"
                        )
                    old = (
                        await conn.execute(
                            select(message_identity).where(
                                message_identity.c.namespace == namespace,
                                message_identity.c.message_id == mid,
                            )
                        )
                    ).first()
                    if old is not None:
                        if json.loads(old.data).get("erased") is True:
                            raise ValueError(f"message_id {mid!r} was erased and cannot be reused")
                        if (
                            old.intent_id == change.receipt.intent_id
                            and old.conversation_id == cid
                            and old.session_id == sid
                        ):
                            continue
                        raise ValueError(
                            f"message_id identity conflict for {mid!r} in namespace {namespace!r}"
                        )
                    conversation = (
                        await conn.execute(
                            select(conversation_identity).where(
                                conversation_identity.c.namespace == namespace,
                                conversation_identity.c.conversation_id == cid,
                            )
                        )
                    ).first()
                    if conversation is not None and conversation.session_id != sid:
                        raise ValueError(
                            f"conversation_id {cid!r} is already assigned to session_id {conversation.session_id!r}"
                        )
                    if conversation is None:
                        await conn.execute(
                            insert(conversation_identity).values(
                                namespace=namespace,
                                conversation_id=cid,
                                session_id=sid,
                                intent_id=change.receipt.intent_id,
                            )
                        )
                    await conn.execute(
                        insert(message_identity).values(
                            namespace=namespace,
                            message_id=mid,
                            conversation_id=cid,
                            session_id=sid,
                            intent_id=change.receipt.intent_id,
                            write_id=item["wid"],
                            record_id=None,
                            data=json.dumps(dict(meta), sort_keys=True),
                        )
                    )
    except IntegrityError as exc:
        raise ValueError("message or conversation identity conflict in namespace") from exc


async def applied_intent(
    engine: AsyncEngine,
    *,
    intent_id: str,
    skey: str,
    source_id: str,
    real_ids: Mapping[str, str],
    receipt: Receipt,
    readiness: Readiness,
) -> None:
    """The apply finished: real record ids into the write rows, the live state, the receipt; the
    apply payload (source text) is dropped. One transaction."""
    async with engine.begin() as conn:
        for wid, rid in real_ids.items():
            await conn.execute(
                update(intake_write)
                .where(intake_write.c.scope_key == skey, intake_write.c.write_id == wid)
                .values(record_id=rid)
            )
            await conn.execute(
                update(message_identity)
                .where(
                    message_identity.c.intent_id == intent_id, message_identity.c.write_id == wid
                )
                .values(record_id=rid)
            )
        row = (
            await conn.execute(
                select(intake_source.c.state).where(
                    intake_source.c.scope_key == skey, intake_source.c.source_id == source_id
                )
            )
        ).first()
        if row is not None and real_ids:
            state = _state_from_json(row.state)
            live = tuple(
                LiveSegment(
                    s.seg_index,
                    s.write_id,
                    s.event_id,
                    real_ids.get(s.write_id, s.record_id),
                    s.content_fp,
                    s.version,
                    s.intent_id,
                )
                for s in state.live
            )
            await conn.execute(
                update(intake_source)
                .where(intake_source.c.scope_key == skey, intake_source.c.source_id == source_id)
                .values(
                    state=_state_to_json(
                        SourceState(
                            state.source_id, state.kind, state.latest_version, state.versions, live
                        )
                    )
                )
            )
        await conn.execute(
            update(intake_intent)
            .where(intake_intent.c.intent_id == intent_id)
            .values(
                status="applied",
                apply=None,
                receipt=receipt.model_dump_json(),
                readiness=readiness.model_dump_json(),
            )
        )


async def set_readiness(engine: AsyncEngine, intent_id: str, readiness: Readiness) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            update(intake_intent)
            .where(intake_intent.c.intent_id == intent_id)
            .values(readiness=readiness.model_dump_json())
        )


async def forget_intent(engine: AsyncEngine, intent_id: str) -> None:  # pragma: no cover - ops hook
    async with engine.begin() as conn:
        await conn.execute(delete(intake_intent).where(intake_intent.c.intent_id == intent_id))
