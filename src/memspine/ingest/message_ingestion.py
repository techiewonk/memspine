"""One canonical message flow: indexed identity -> validation -> durable acceptance -> apply."""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any

from memspine.ingest import gateway
from memspine.ingest.message_identity import normalise_messages
from memspine.ingest.message_retry import input_fingerprint, prepare_retries
from memspine.ingest.message_schema import ValidatedMessageBatch
from memspine.ingest.message_store import lookup_conversations, lookup_messages
from memspine.observability import tap
from memspine.store.records import MemoryRecord
from memspine.store.scope import validate_namespace


async def ingest_messages(
    eng: Any,
    messages: Sequence[Mapping[str, Any]],
    namespace: str,
    actor: str,
    session_id: str | None,
    channel: str,
    group_id: str | None,
    tags: list[str] | None,
    valid_from: datetime | None,
    *,
    conversation_id: str | None = None,
) -> list[MemoryRecord]:
    """Resolve the batch once and submit only immutable, resolved message data.

    SQL identity reservations are committed with the acceptance intent. A concurrent
    identical submission waits for the owning call rather than applying its active
    intent again. Recovery of a crashed process happens at engine startup.
    """
    ns = validate_namespace(namespace)
    if not messages:
        return []
    locks = eng.__dict__.setdefault("_message_ingest_locks", {})
    async with locks.setdefault(ns, asyncio.Lock()):
        gw = gateway.gateway_of(eng)
        db = await gw._engine()
        if getattr(gw, "_message_recovery_needed", False):
            await gateway.recover(eng)
            gw._message_recovery_needed = False
        for attempt in range(250):
            try:
                result = await _ingest_once(
                    eng,
                    db,
                    messages,
                    ns,
                    actor,
                    session_id,
                    channel,
                    group_id,
                    tags,
                    valid_from,
                    conversation_id,
                )
                if result is not None:
                    return result
            except ValueError as exc:
                # A competing transaction may reserve an ID between lookup and commit.
                if "identity conflict" not in str(exc):
                    raise
                if attempt == 249:
                    raise
            await asyncio.sleep(0.02)
        raise TimeoutError("message acceptance is still pending; retry with the same message IDs")


async def _ingest_once(
    eng: Any,
    db: Any,
    messages: Sequence[Mapping[str, Any]],
    ns: str,
    actor: str,
    session_id: str | None,
    channel: str,
    group_id: str | None,
    tags: list[str] | None,
    valid_from: datetime | None,
    conversation_id: str | None,
) -> list[MemoryRecord] | None:
    storage = eng._require_started()
    requested: list[str] = []
    conversations = [conversation_id] if conversation_id else []
    for raw in messages:
        if not isinstance(raw, Mapping):
            raise ValueError("messages must be mappings")
        for key in ("message_id", "reply_to"):
            value = raw.get(key)
            if isinstance(value, str):
                requested.append(value)
        cid = raw.get("conversation_id")
        if isinstance(cid, str):
            conversations.append(cid)
    identities = await lookup_messages(db, ns, requested)
    # Only a committed-but-unapplied owner needs waiting. Applied skipped records
    # (e.g. excluded system messages) are still reserved identities, not new input.
    if any(
        r.get("intent_status") == "accepted" and not r.get("record_id") for r in identities.values()
    ):
        return None
    owners = await lookup_conversations(db, ns, conversations)
    stored = [
        r
        for r in [
            await storage.get_record(m["record_id"])
            for m in identities.values()
            if m.get("record_id")
        ]
        if r is not None
    ]
    inputs, reused = prepare_retries(messages, stored, session_id, conversation_id, identities)
    if valid_from is not None:
        for turn in inputs:
            if not turn.get("created_at") and not turn.get("timestamp"):
                turn["created_at"] = valid_from.isoformat()
    accepted_at = eng._clock()
    turns = normalise_messages(
        inputs,
        namespace=ns,
        session_id=session_id,
        conversation_id=conversation_id,
        existing=identities.values(),
        existing_conversations={cid: row["session_id"] for cid, row in owners.items()},
        retry_ids=reused.keys(),
        now=accepted_at,
    )
    known = {mid: str(m["record_id"]) for mid, m in identities.items() if m.get("record_id")}
    positions: dict[str, int] = {}
    fresh = [t for t in turns if t["message_id"] not in reused]
    originals = {str(t["message_id"]): raw for t, raw in zip(turns, messages, strict=True)}
    for i, turn in enumerate(fresh):
        public: dict[str, Any] = {
            k: turn[k]
            for k in (
                "message_id",
                "session_id",
                "conversation_id",
                "sender",
                "created_at",
                "updated_at",
                "metadata",
                "reply_to",
            )
            if k in turn
        }
        public["ingested_at"] = accepted_at.isoformat()
        public["created_at_generated"] = turn["_created_at_generated"]
        public["input_fingerprints"] = {
            k: input_fingerprint(v)
            for k, v in originals[str(turn["message_id"])].items()
            if not k.startswith("_")
        }
        turn["_message_envelope"] = public
        parent = turn.get("reply_to")
        if parent is not None:
            if parent not in positions and parent not in known:
                raise ValueError("reply parent has no available raw record")
            turn["reply_to"] = positions[parent] if parent in positions else known[parent]
        positions[str(turn["message_id"])] = i
    tap.event(
        "ingest.messages.resolved",
        namespace=ns,
        messages=len(turns),
        sessions=len({t["session_id"] for t in turns}),
        conversations=len({t["conversation_id"] for t in turns}),
    )
    added: list[MemoryRecord] = []
    if fresh:
        gw = gateway.gateway_of(eng)
        gw._message_recovery_needed = True
        try:
            added = await gateway.ingest_resolved_messages(
                eng,
                ValidatedMessageBatch.from_resolved(fresh),
                ns,
                actor,
                session_id,
                channel,
                group_id,
                tags,
                valid_from,
            )
        except ValueError as exc:
            # This is the known pre-commit reservation race. No message intent was accepted,
            # so a later call must not run global recovery just because this attempt started.
            if "identity conflict" in str(exc):
                gw._message_recovery_needed = False
            raise
        gw._message_recovery_needed = False
    landed = {r.source.message["message_id"]: r for r in added if r.source.message}
    landed.update({mid: r for mid, r in reused.items() if r is not None})
    return [landed[t["message_id"]] for t in turns if t["message_id"] in landed]
