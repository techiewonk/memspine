"""Trust: audit moved out of the engine (F-12)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from memspine.config import constants
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.scope import validate_namespace
from memspine.trust.pipeline import audit_v2
from memspine.trust.privacy import (
    AUDIT_GENESIS,
    AUDIT_KINDS,
    AuditChainReport,
    chain_digest,
    current_principal,
    current_purpose,
)
from memspine.trust.privacy import verify_audit_chain as lib_verify_audit_chain


async def _audit_head_hash(eng: Any) -> str:
    """The current chain head, loaded once from the log (the last audit event)."""
    if eng._audit_head is None:
        storage = eng._require_started()
        head = AUDIT_GENESIS
        after = 0
        while True:
            batch = await storage.read_events(after_seq=after, limit=1000)
            if not batch:
                break
            for event in batch:
                chain = event.payload.get("chain")
                if event.kind in AUDIT_KINDS and isinstance(chain, dict):
                    head = str(chain.get("hash", head))
            after = max(e.seq for e in batch if e.seq is not None)
        eng._audit_head = head
    return eng._audit_head


async def _append_audit(
    eng: Any, kind: EventKind, ns: str, actor: str, body: dict[str, object]
) -> None:
    """#49: append one chained audit event (serialized, so the chain is linear)."""
    async with eng._audit_lock:
        prev = await eng._audit_head_hash()
        payload: dict[str, object] = {**body, "at": eng._clock().isoformat()}
        digest = chain_digest(prev, kind.value, ns, actor, payload)
        payload["chain"] = {"prev": prev, "hash": digest}
        await eng._append_and_project(
            MemoryEvent(kind=kind, namespace=ns, actor=actor, payload=payload)
        )
        eng._audit_head = digest


async def bulk_read_alerts(
    eng: Any,
    namespace: str = "default",
    *,
    window: timedelta = timedelta(hours=1),
    max_records: int = constants.BULK_READ_MAX_RECORDS,
) -> list[dict[str, object]]:
    """G31 (plan v3.2): principals whose reads in the last ``window`` touched more
    than ``max_records`` distinct records of ``namespace`` (an extraction pattern:
    membership inference or scraping by repeated queries). Built from the
    ``READ_AUDIT`` events, so it needs ``audit.reads``; read-only."""
    ns = validate_namespace(namespace)
    since = eng._clock() - window
    seen: dict[str, set[str]] = {}
    reads: dict[str, int] = {}
    storage = eng._require_started()
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        for event in batch:
            if event.kind is not EventKind.READ_AUDIT or event.namespace != ns:
                continue
            at = event.payload.get("at")
            if isinstance(at, str) and datetime.fromisoformat(at) < since:
                continue
            who = str(event.payload.get("principal") or "anonymous")
            seen.setdefault(who, set()).update(
                str(r) for r in event.payload.get("record_ids") or []
            )
            reads[who] = reads.get(who, 0) + 1
        after = max(e.seq for e in batch if e.seq is not None)
    return [
        {"principal": who, "distinct_records": len(ids), "reads": reads[who]}
        for who, ids in sorted(seen.items())
        if len(ids) > max_records
    ]


async def _audit_read(eng: Any, verb: str, namespace: str, record_ids: Sequence[str]) -> None:
    """#49: a READ_AUDIT event for one read verb (``audit.reads`` only)."""
    if audit_v2.enabled(eng):  # T5-05: LIB-41 hash-chained records, the profile decides
        return await audit_v2.read(eng, verb, namespace, record_ids)
    if not eng._config().audit.reads:
        return
    principal = current_principal() or "anonymous"
    await eng._append_audit(
        EventKind.READ_AUDIT,
        validate_namespace(namespace),
        principal,
        {
            "verb": verb,
            "principal": principal,
            "purpose": current_purpose(),
            "record_ids": list(record_ids),
        },
    )


async def _audit_action(
    eng: Any,
    action: str,
    ns: str,
    record_ids: Sequence[str],
    *,
    actor: str,
    reason: str | None,
    **extra: object,
) -> None:
    """#49: an AUDIT event for a governance action (``audit.actions`` only)."""
    if audit_v2.enabled(eng):  # T5-05: LIB-41 hash-chained records, the profile decides
        return await audit_v2.action(eng, action, ns, record_ids, actor=actor, reason=reason)
    if not eng._config().audit.actions:
        return
    await eng._append_audit(
        EventKind.AUDIT,
        ns,
        actor,
        {
            "action": action,
            "actor": actor,
            "principal": current_principal(),
            "reason": reason,
            "record_ids": list(record_ids),
            **extra,
        },
    )


async def verify_audit_chain(eng: Any) -> AuditChainReport:
    """#49: validate the hash chain over the persisted audit events.

    Each ``memory.read_audit`` / ``memory.audit`` event stores the previous
    event's hash; an edited, inserted or removed audit event breaks a link.
    A rolling log anchors at its oldest surviving audit event; an ephemeral
    log persists nothing, so its report covers no events."""
    storage = eng._require_started()
    events: list[MemoryEvent] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            break
        events.extend(e for e in batch if e.kind in AUDIT_KINDS)
        after = max(e.seq for e in batch if e.seq is not None)
    anchored = eng._config().event_log.mode is EventLogMode.FULL
    return lib_verify_audit_chain(events, anchored=anchored)


async def audit_chain_ok(eng: Any) -> bool:
    """#49: True when the audit hash chain verifies (see :meth:`verify_audit_chain`); under
    ``audit.chain_v2`` the LIB-41 chain (:meth:`verify_audit_chain_v2`)."""
    if audit_v2.enabled(eng):
        return (await audit_v2.verify(eng)).ok
    return (await eng.verify_audit_chain()).ok
