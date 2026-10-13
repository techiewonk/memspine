"""Trust: hash-chained audit records (T5-05; LIB-41 ``trust/retention.py``), behind
``audit.chain_v2``.

With the flag on, ``Engine._audit_action`` and ``Engine._audit_read`` seal one LIB-41
``AuditRecord`` instead of a ``memory.audit`` / ``memory.read_audit`` event: who, which action,
which ids, purpose, time, outcome, never content (the free-form ``extra`` fields and the reason
text are dropped; the reason is reduced to a short code). The records ride the event log as MARKER
events and form one chain per engine (``prev_hash``). What is audited follows the retention
profile (:func:`memspine.trust.retention.must_audit`): erase, consent and admin always, a
correction, reads only under ``audit.reads``. :func:`verify` re-reads the chain and runs
``verify_chain``.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.observability import tap
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.scope import validate_namespace
from memspine.trust.privacy import current_principal, current_purpose
from memspine.trust.retention import (
    GENESIS_HASH,
    AuditAction,
    AuditEntry,
    AuditLog,
    AuditRecord,
    ChainReport,
    must_audit,
    verify_chain,
)
from memspine.trust.retention_profile import build_profile

__all__ = ["MARKER_AUDIT", "action", "append", "enabled", "read", "verify"]

MARKER_AUDIT = "audit_chain_v2"

_ID_BAD = re.compile(r"[^A-Za-z0-9._:@/\-]")
_CODE_BAD = re.compile(r"[^a-z0-9_.:\-]")
_PURPOSE_OK = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._:*\-]{0,63}$")

_ERASE = frozenset({"forget", "erase_subject", "erase_namespace", "retention_expiry"})
_CONSENT = frozenset({"grant", "revoke", "share", "unshare", "subscribe", "unsubscribe"})


def enabled(eng: Any) -> bool:
    return bool(eng._config().audit.chain_v2)


def _ident(value: str | None, default: str) -> str:
    text = _ID_BAD.sub("_", str(value or ""))[:128].lstrip("._:@/-")
    return text or default


def _code(value: str | None) -> str | None:
    text = _CODE_BAD.sub("_", str(value or "").lower())[:64].lstrip("_.:-")
    return text or None


def _classify(name: str) -> tuple[AuditAction, str]:
    op = _code(name) or "admin"
    if name in _ERASE:
        return "erase", op
    if name == "correct":
        return "write", "correct"
    if name in _CONSENT:
        return "consent", op
    return "admin", op


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _entry(
    eng: Any,
    kind: AuditAction,
    op: str,
    ns: str,
    ids: Sequence[str],
    actor: str,
    reason: str | None,
) -> AuditEntry:
    purpose = current_purpose() or "*"
    return AuditEntry(
        actor=_ident(actor, "system"),
        action=kind,
        op=_code(op) or "op",
        ids=tuple(_ident(i, "id") for i in ids),
        purpose=purpose if _PURPOSE_OK.match(purpose) else "*",
        at=_aware(eng._clock()),
        reason=_code(reason),
        tenant=_ident(ns, "default"),
    )


async def _records(eng: Any) -> list[AuditRecord]:
    storage = eng._require_started()
    out: list[AuditRecord] = []
    after = 0
    while True:
        batch = await storage.read_events(after_seq=after, limit=1000)
        if not batch:
            return out
        for ev in batch:
            if ev.kind is EventKind.MARKER and ev.payload.get("marker") == MARKER_AUDIT:
                out.append(AuditRecord.model_validate(ev.payload["record"]))
        after = max(e.seq for e in batch if e.seq is not None)


async def _head(eng: Any) -> tuple[int, str, datetime | None]:
    """(last seq, last hash, last time), loaded once from the log."""
    head = eng.__dict__.get("_audit_v2_head")
    if head is None:
        seq, digest, at = 0, GENESIS_HASH, None
        for rec in await _records(eng):
            seq, digest, at = rec.seq, rec.hash, rec.at
        head = eng.__dict__["_audit_v2_head"] = (seq, digest, at)
    return head  # type: ignore[no-any-return]


async def append(eng: Any, entry: AuditEntry) -> AuditRecord | None:
    """Seal ``entry`` into the chain when the profile audits that action. Serialized."""
    profile = build_profile(eng._config())
    if not must_audit(profile, entry.action, entry.op):
        if tap.enabled():
            tap.event("trust.audit.append", appended=False, action=entry.action, op=entry.op)
        return None
    async with eng._audit_lock:
        seq, digest, last_at = await _head(eng)
        if last_at is not None and entry.at < last_at:
            entry = entry.model_copy(update={"at": last_at})  # the chain checks time order
        sealed = AuditLog(anchor_hash=digest, anchor_seq=seq).append(entry).records[0]
        await eng._append_and_project(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace=entry.tenant or "default",
                actor="system",
                payload={"marker": MARKER_AUDIT, "record": sealed.model_dump(mode="json")},
            )
        )
        eng.__dict__["_audit_v2_head"] = (sealed.seq, sealed.hash, sealed.at)
    if tap.enabled():
        tap.event(
            "trust.audit.append",
            appended=True,
            action=sealed.action,
            op=sealed.op,
            seq=sealed.seq,
            ids=len(sealed.ids),
        )
    return sealed


async def action(
    eng: Any, name: str, ns: str, record_ids: Sequence[str], *, actor: str, reason: str | None
) -> None:
    kind, op = _classify(name)
    await append(eng, _entry(eng, kind, op, ns, record_ids, actor, reason))


async def read(eng: Any, verb: str, namespace: str, record_ids: Sequence[str]) -> None:
    principal = current_principal() or "anonymous"
    await append(
        eng,
        _entry(eng, "read", verb, validate_namespace(namespace), record_ids, principal, None),
    )


async def verify(eng: Any) -> ChainReport:
    """Re-read the persisted chain and verify it (seq, links, hashes, time order). A rolling log
    anchors at its oldest surviving record; an ephemeral log persists none."""
    records = await _records(eng)
    if eng._config().event_log.mode is not EventLogMode.FULL and records:
        first = records[0]
        report = verify_chain(records, anchor_hash=first.prev_hash, anchor_seq=first.seq - 1)
    else:
        report = verify_chain(records)
    if tap.enabled():
        tap.event(
            "trust.audit.verify",
            ok=report.ok,
            checked=report.checked,
            broken_at=report.broken_at,
            reason=report.reason,
        )
    return report
