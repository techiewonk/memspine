"""Durable ledger for the intake gateway (W2-01; LIB-30 ``IntakeStore`` over SQL).

``IntakeBook`` (``ingest.intake``) is pure and synchronous; its state lives in an injected store. This
module persists that state in the engine's own database (three small tables on their own
``MetaData``, created on first use, so the Alembic tree and the v1 baseline are untouched):

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

from sqlalchemy import Column, MetaData, String, Table, Text, delete, insert, select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.ingest.intake import (
    InMemoryIntakeStore,
    IntakeChange,
    LiveSegment,
    SourceState,
    VersionRecord,
)
from memspine.schemas.public.receipt import Readiness, Receipt

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

intake_metadata = MetaData()

intake_source = Table(
    "intake_source",
    intake_metadata,
    Column("scope_key", String, primary_key=True),
    Column("source_id", String, primary_key=True),
    Column("state", Text, nullable=False),
)
intake_write = Table(
    "intake_write",
    intake_metadata,
    Column("scope_key", String, primary_key=True),
    Column("write_id", String, primary_key=True),
    Column("source_id", String, nullable=False),
    Column("record_id", String, nullable=True),
    Column("data", Text, nullable=False),
)
intake_intent = Table(
    "intake_intent",
    intake_metadata,
    Column("intent_id", String, primary_key=True),
    Column("scope_key", String, nullable=False),
    Column("namespace", String, nullable=False),
    Column("source_id", String, nullable=False),
    Column("status", String, nullable=False),
    Column("receipt", Text, nullable=False),
    Column("readiness", Text, nullable=False),
    Column("apply", Text, nullable=True),
    Column("created_at", String, nullable=False),
)

INTAKE_TABLES: tuple[str, ...] = ("intake_source", "intake_write", "intake_intent")


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
            rows = (
                await conn.execute(
                    select(
                        intake_intent.c.intent_id,
                        intake_intent.c.receipt,
                        intake_intent.c.readiness,
                    ).where(intake_intent.c.intent_id.in_(sorted(intent_ids)))
                )
            ).all()
            for r in rows:
                ov._receipts[r.intent_id] = Receipt.model_validate_json(r.receipt).model_copy(
                    update={"readiness": Readiness.model_validate_json(r.readiness)}
                )
                ov._readiness[r.intent_id] = Readiness.model_validate_json(r.readiness)
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
