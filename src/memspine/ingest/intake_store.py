"""Durable ledger for the intake gateway (W2-01; LIB-30 ``IntakeStore`` over the intake port).

``IntakeBook`` (``ingest.intake``) is pure and synchronous; its state lives in an injected store. This
module persists that state through the records store's intake ledger
(:class:`~memspine.services.storage.intake_port.IntakeLedger`, RM-137); the SQL ledger's tables
are part of the current fresh SQL baseline:

``intake_source``  one row per (scope, source): latest version, version records, live segments (JSON).
``intake_write``   one row per S3-3 write id, with the real record id once the record exists.
``intake_intent``  one row per accepted envelope: receipt, readiness, the apply payload while the
                   records are still being written (``status`` ``accepted`` -> ``applied``).

A call is *committed* when :func:`commit` returns: the source state, the new write ids, the receipt
and the apply payload are written in ONE transaction, so a crash leaves either nothing or all of it.
The apply payload is what lets the next ``start()`` finish an interrupted write; it is cleared (set to
NULL) as soon as the records exist, so no source text stays in the ledger.

This module owns the state codec and the message-identity extraction; the port owns storage.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict
from typing import Any

from memspine.ingest.intake import (
    InMemoryIntakeStore,
    IntakeChange,
    LiveSegment,
    SourceState,
    VersionRecord,
)
from memspine.schemas.durable.ingestion import RequiredDerivation
from memspine.schemas.public.receipt import Readiness, Receipt
from memspine.services.storage.intake_port import IntakeLedger, MessageClaim

__all__ = [
    "Overlay",
    "applied_intent",
    "applied_scope_keys",
    "commit",
    "ensure_schema",
    "intents_by_id",
    "load_overlay",
    "message_claims",
    "pending_intents",
    "record_ids",
    "set_readiness",
]


async def ensure_schema(ledger: IntakeLedger) -> None:
    await ledger.ensure_schema()


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
    ledger: IntakeLedger, skey: str, source_id: str, write_ids: Sequence[str]
) -> Overlay:
    """Source state, the named write ids and every receipt they point at, as a sync store."""
    ov = Overlay()

    def intent_ids_of(state_json: str | None, writes: Mapping[str, str]) -> set[str]:
        intent_ids: set[str] = set()
        if state_json is not None:
            state = _state_from_json(state_json)
            ov._sources[(skey, source_id)] = state
            intent_ids.update(v.intent_id for v in state.versions.values())
        for wid, data in writes.items():
            live = _live_from_json(json.loads(data))
            ov._writes[(skey, wid)] = live
            intent_ids.add(live.intent_id)
        return intent_ids

    found = await ledger.load_slice(skey, source_id, write_ids, intent_ids_of)
    for intent_id, (receipt, readiness) in found.intents.items():
        ov._receipts[intent_id] = Receipt.model_validate_json(receipt).model_copy(
            update={"readiness": Readiness.model_validate_json(readiness)}
        )
        ov._readiness[intent_id] = Readiness.model_validate_json(readiness)
    return ov


async def intents_by_id(ledger: IntakeLedger, ids: Iterable[str]) -> dict[str, dict[str, Any]]:
    return await ledger.intents_by_id(ids)


async def pending_intents(ledger: IntakeLedger) -> list[dict[str, Any]]:
    """Accepted-but-not-applied intents, oldest first (a restart finishes them in order)."""
    return await ledger.pending_intents()


async def applied_scope_keys(ledger: IntakeLedger) -> list[str]:
    """Scopes with committed raw intents, used by restart reconciliation."""
    return await ledger.applied_scope_keys()


async def record_ids(ledger: IntakeLedger, skey: str, write_ids: Sequence[str]) -> dict[str, str]:
    if not write_ids:
        return {}
    return await ledger.record_ids(skey, write_ids)


# --------------------------------------------------------------------------------------------- writes
def message_claims(apply_payload: str) -> list[MessageClaim]:
    """The public message identities an acceptance reserves (``messages`` adapter only)."""
    payload = json.loads(apply_payload)
    claims: list[MessageClaim] = []
    if payload.get("adapter") != "messages":
        return claims
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
        if not (
            isinstance(mid, str)
            and mid
            and isinstance(cid, str)
            and cid
            and isinstance(sid, str)
            and sid
        ):
            raise ValueError(
                "message identity requires message_id, conversation_id, and session_id"
            )
        claims.append(
            MessageClaim(
                message_id=mid,
                conversation_id=cid,
                session_id=sid,
                write_id=item["wid"],
                data=json.dumps(dict(meta), sort_keys=True),
            )
        )
    return claims


async def commit(
    ledger: IntakeLedger,
    change: IntakeChange,
    *,
    namespace: str,
    apply_payload: str,
    created_at: str,
    required_derivation: RequiredDerivation,
) -> None:
    """The one transaction of an accept: source state, new write ids, receipt, apply payload,
    message identities."""
    state = SourceState(
        change.source_id,
        change.kind,
        change.version,
        {change.version: change.version_record},
        change.live,
    )

    def merge_state(previous: str | None) -> str:
        if previous is None:
            return _state_to_json(state)
        prev = _state_from_json(previous)
        versions = {**prev.versions, change.version: change.version_record}
        return _state_to_json(
            SourceState(
                change.source_id,
                change.kind,
                max(prev.latest_version, change.version),
                versions,
                change.live,
            )
        )

    fresh = set(change.new_write_ids)
    await ledger.record_intent(
        scope_key=change.scope_key,
        source_id=change.source_id,
        merge_state=merge_state,
        new_writes=[
            (seg.write_id, json.dumps(_live_to_json(seg), sort_keys=True))
            for seg in change.live
            if seg.write_id in fresh
        ],
        intent_id=change.receipt.intent_id,
        namespace=namespace,
        receipt=change.receipt,
        readiness=change.readiness,
        required_derivation=required_derivation,
        apply_payload=apply_payload,
        created_at=created_at,
        messages=message_claims(apply_payload),
    )


async def applied_intent(
    ledger: IntakeLedger,
    *,
    intent_id: str,
    skey: str,
    source_id: str,
    real_ids: Mapping[str, str],
    receipt: Receipt,
    readiness: Readiness,
    required_derivation: RequiredDerivation,
) -> None:
    """The apply finished: real record ids into the write rows, the live state, the receipt; the
    apply payload (source text) is dropped. One transaction."""

    def relink_state(stored: str) -> str:
        state = _state_from_json(stored)
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
        return _state_to_json(
            SourceState(state.source_id, state.kind, state.latest_version, state.versions, live)
        )

    await ledger.apply_intent(
        intent_id=intent_id,
        scope_key=skey,
        source_id=source_id,
        real_ids=real_ids,
        relink_state=relink_state,
        receipt=receipt,
        readiness=readiness,
        required_derivation=required_derivation,
    )


async def set_readiness(ledger: IntakeLedger, intent_id: str, readiness: Readiness) -> None:
    await ledger.set_readiness(intent_id, readiness)


async def forget_intent(
    ledger: IntakeLedger, intent_id: str
) -> None:  # pragma: no cover - ops hook
    await ledger.forget_intent(intent_id)
