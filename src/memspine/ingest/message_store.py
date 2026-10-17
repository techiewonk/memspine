"""Durable lookups for public message identities stored by the intake transaction."""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import orjson
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine

from memspine.ingest.intake_store import (
    conversation_identity,
    intake_intent,
    message_identity,
)


async def lookup_messages(
    db: AsyncEngine, namespace: str, message_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Return requested identities and record IDs without scanning raw records."""
    wanted = list(dict.fromkeys(message_ids))
    if not wanted:
        return {}
    async with db.connect() as conn:
        rows = (
            await conn.execute(
                select(message_identity, intake_intent.c.status.label("intent_status"))
                .outerjoin(
                    intake_intent,
                    intake_intent.c.intent_id == message_identity.c.intent_id,
                )
                .where(
                    message_identity.c.namespace == namespace,
                    message_identity.c.message_id.in_(wanted),
                )
            )
        ).all()
    return {
        row.message_id: {
            **json.loads(row.data),
            "message_id": row.message_id,
            "conversation_id": row.conversation_id,
            "session_id": row.session_id,
            "intent_id": row.intent_id,
            "record_id": row.record_id,
            "intent_status": row.intent_status,
        }
        for row in rows
    }


async def lookup_conversations(
    db: AsyncEngine, namespace: str, conversation_ids: Sequence[str]
) -> dict[str, dict[str, Any]]:
    """Return lightweight namespace-scoped conversation/session reservations."""
    wanted = list(dict.fromkeys(conversation_ids))
    if not wanted:
        return {}
    async with db.connect() as conn:
        rows = (
            await conn.execute(
                select(conversation_identity).where(
                    conversation_identity.c.namespace == namespace,
                    conversation_identity.c.conversation_id.in_(wanted),
                )
            )
        ).all()
    return {
        row.conversation_id: {
            "conversation_id": row.conversation_id,
            "session_id": row.session_id,
            "intent_id": row.intent_id,
        }
        for row in rows
    }


async def tombstone_records(db: AsyncEngine, record_ids: Sequence[str]) -> int:
    """Redact metadata for hard-forgotten records while retaining non-reusable IDs."""
    wanted = list(dict.fromkeys(record_ids))
    if not wanted:
        return 0
    async with db.begin() as conn:
        result = await conn.execute(
            update(message_identity)
            .where(message_identity.c.record_id.in_(wanted))
            .values(
                intent_id=None,
                write_id=None,
                record_id=None,
                data=json.dumps({"erased": True}, sort_keys=True),
            )
        )
        return int(result.rowcount or 0)


async def backfill_from_storage(storage: Any) -> int:
    """Index existing source envelopes without reading/copying record content.

    This is a startup repair for databases created before the identity projection existed. IDs
    already present (including erased tombstones) are never replaced.
    """
    from memspine.services.storage.sqlite.schema import memory_records

    db = storage._client.engine
    async with db.connect() as conn:
        rows = (
            await conn.execute(
                select(
                    memory_records.c.record_id,
                    memory_records.c.namespace,
                    memory_records.c.source,
                ).where(memory_records.c.memory_type == "episodic")
            )
        ).all()
    # Gather only public envelope fields; message identity metadata never includes content.
    found: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        source = orjson.loads(row.source)
        meta = source.get("message") if isinstance(source, dict) else None
        if not isinstance(meta, dict):
            continue
        mid, cid, sid = meta.get("message_id"), meta.get("conversation_id"), meta.get("session_id")
        if all(isinstance(value, str) and value for value in (mid, cid, sid)):
            public = {
                key: meta[key]
                for key in (
                    "message_id",
                    "conversation_id",
                    "session_id",
                    "sender",
                    "created_at",
                    "updated_at",
                    "metadata",
                    "reply_to",
                    "ingested_at",
                    "created_at_generated",
                    "input_fingerprints",
                )
                if key in meta
            }
            found.append((str(row.record_id), str(row.namespace), public))
    inserted = 0
    async with db.begin() as conn:
        for rid, namespace, meta in found:
            mid, cid, sid = meta["message_id"], meta["conversation_id"], meta["session_id"]
            existing = (
                await conn.execute(
                    select(message_identity.c.message_id).where(
                        message_identity.c.namespace == namespace,
                        message_identity.c.message_id == mid,
                    )
                )
            ).first()
            if existing is not None:
                continue
            reservation = (
                await conn.execute(
                    select(conversation_identity.c.session_id).where(
                        conversation_identity.c.namespace == namespace,
                        conversation_identity.c.conversation_id == cid,
                    )
                )
            ).first()
            if reservation is not None and reservation.session_id != sid:
                raise ValueError(
                    f"conversation_id {cid!r} maps to multiple sessions in namespace {namespace!r}"
                )
            if reservation is None:
                await conn.execute(
                    conversation_identity.insert().values(
                        namespace=namespace,
                        conversation_id=cid,
                        session_id=sid,
                        intent_id=None,
                    )
                )
            await conn.execute(
                message_identity.insert().values(
                    namespace=namespace,
                    message_id=mid,
                    conversation_id=cid,
                    session_id=sid,
                    intent_id=None,
                    write_id=None,
                    record_id=rid,
                    data=json.dumps(meta, sort_keys=True),
                )
            )
            inserted += 1
    return inserted
