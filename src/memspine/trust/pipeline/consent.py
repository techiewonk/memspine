# ruff: noqa: E501
"""Trust: consent moved out of the engine (F-12); privacy tiers and the consent registry (T5-03).

With ``consent.v2`` off, :func:`_consent_ok` is the #50 purpose gate over ``consent_tags``, untouched.
With it on, the LIB-18 library (``trust.consent``) decides: ``may_read`` on every read, ``may_store`` at
the write door, ``may_derive`` at the derived-write door. The registry is process-local state held on the
engine and mirrored into the event log as audit-only MARKER events, so :func:`ensure_registry` rebuilds
it after a restart (revocations are never forgotten).

Mapping of an engine record to the library (S3-1, scope is tenant plus owner): tenant and owner are the
record's namespace, the tier comes from ``pii_tier`` (``none/low/high/regulated``), the stored purposes
are the record's ``consent_tags`` (none, or ``*``, mean the broadest purpose), the reader's purpose is
``current_purpose()`` (none means the broadest, which only a ``*`` record serves).
"""

from __future__ import annotations

from typing import Any

from memspine.observability import tap
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord
from memspine.trust.consent import (
    PURPOSE_ANY,
    AdmissionDecision,
    ConsentRegistry,
    Reason,
    RecordMeta,
    Viewer,
    ViewerKind,
    may_derive,
    may_read,
    may_store,
    tier_from_pii,
)
from memspine.trust.pipeline.erasure_v2 import visible_id
from memspine.trust.privacy import PURPOSE_NONE, current_purpose, purpose_allows

_REGISTRY_KEY = "_consent_registry"
_LOADED_KEY = "_consent_registry_loaded"
_MARKER = "consent_registry"


def _consent_ok(eng: Any, record: MemoryRecord) -> bool:
    """#50: whether the current read's purpose may see ``record``. Also the one gate every read
    leg ends in, so it carries the T5-04 / POL-1 erasure check: an erased or hidden id is never
    visible (a no-op until ``privacy.erasure_v2`` erased something)."""
    if not visible_id(eng, record.record_id):
        return False
    cfg = eng._config().consent
    if cfg.v2:
        return read_decision(eng, record, current_purpose()).allowed
    return purpose_allows(record, current_purpose(), cfg)


# --------------------------------------------------------------------------- registry


def registry(eng: Any) -> ConsentRegistry:
    return eng.__dict__.get(_REGISTRY_KEY) or ConsentRegistry()


def generation(eng: Any) -> int | None:
    """The registry generation for cache keys (ER-31); None when ``consent.v2`` is off."""
    if not eng._config().consent.v2:
        return None
    return registry(eng).generation


async def ensure_registry(eng: Any) -> ConsentRegistry:
    """Rebuild the registry from the log once per engine (no-op with ``consent.v2`` off)."""
    if not eng._config().consent.v2 or eng.__dict__.get(_LOADED_KEY):
        return registry(eng)
    reg = ConsentRegistry()
    storage = eng._require_started()
    after = 0
    while True:
        events = await storage.read_events(after_seq=after, limit=1000)
        if not events:
            break
        for event in events:
            if event.kind is EventKind.MARKER and event.payload.get("marker") == _MARKER:
                reg = _apply(reg, event.payload, event.namespace)
        after = max(e.seq for e in events if e.seq is not None)
    eng.__dict__[_REGISTRY_KEY] = reg
    eng.__dict__[_LOADED_KEY] = True
    return reg


def _apply(reg: ConsentRegistry, body: dict[str, Any], owner: str) -> ConsentRegistry:
    op = body.get("op")
    tenant = str(body.get("tenant", owner))
    if op == "dnd":
        return reg.set_do_not_derive(
            tenant, owner, str(body["subject"]), bool(body.get("on", True))
        )
    grantee = body.get("grantee")
    at = float(body.get("at", 0.0))
    purpose = str(body.get("purpose", PURPOSE_ANY))
    if op == "grant":
        return reg.grant(tenant, owner, purpose, at, None if grantee is None else str(grantee))
    return reg.revoke(tenant, owner, purpose, at, None if grantee is None else str(grantee))


async def _mutate(eng: Any, owner: str, body: dict[str, Any]) -> ConsentRegistry:
    reg = await ensure_registry(eng)
    new = _apply(reg, body, owner)
    eng.__dict__[_REGISTRY_KEY] = new
    eng.__dict__[_LOADED_KEY] = True
    await eng._append_and_project(
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace=owner,
            actor="system",
            payload={"marker": _MARKER, **body},
        )
    )
    if tap.enabled():
        tap.event(
            "trust.consent",
            decision=body["op"],
            reason="registry_change",
            generation=new.generation,
        )
    return new


async def grant(
    eng: Any, owner: str, purpose: str = PURPOSE_ANY, *, grantee: str | None = None
) -> ConsentRegistry:
    """The owner opts in to ``purpose`` (optionally for one agent or ``group:<id>``)."""
    return await _mutate(
        eng,
        owner,
        {"op": "grant", "purpose": purpose, "grantee": grantee, "at": eng._clock().timestamp()},
    )


async def revoke(
    eng: Any, owner: str, purpose: str = PURPOSE_ANY, *, grantee: str | None = None
) -> ConsentRegistry:
    """The owner withdraws ``purpose``: its records stop being served (nothing is deleted)."""
    return await _mutate(
        eng,
        owner,
        {"op": "revoke", "purpose": purpose, "grantee": grantee, "at": eng._clock().timestamp()},
    )


async def set_do_not_derive(eng: Any, owner: str, subject: str, on: bool = True) -> ConsentRegistry:
    """Switch derivation off (or back on) for one subject of ``owner``'s memory (G-11)."""
    return await _mutate(eng, owner, {"op": "dnd", "subject": subject, "on": on})


# --------------------------------------------------------------------------- decisions


def record_meta(record: MemoryRecord, purpose: str) -> RecordMeta:
    return RecordMeta(
        tenant=record.namespace,
        owner=record.namespace,
        tier=tier_from_pii(record.pii_tier),
        purpose=purpose,
    )


def _purposes(eng: Any, record: MemoryRecord) -> list[str] | None:
    """The purposes a record is stored under; None when it is untagged and ``untagged: deny``."""
    tags = [t for t in record.consent_tags if t != PURPOSE_NONE]
    if PURPOSE_ANY in tags:
        return [PURPOSE_ANY]
    if not tags:
        if record.consent_tags:  # only the "no purpose serves it" marker
            return []
        return [PURPOSE_ANY] if eng._config().consent.untagged == "allow" else None
    return tags


def _emit(record_id: str, decision: AdmissionDecision, site: str) -> None:
    if tap.enabled():
        tap.event(
            "trust.consent",
            id=record_id,
            site=site,
            decision="allow" if decision.allowed else "deny",
            reason=decision.reason.value,
            generation=decision.consent_generation,
        )


def read_decision(eng: Any, record: MemoryRecord, reader_purpose: str | None) -> AdmissionDecision:
    """``may_read`` for the owner's own view of ``record`` under ``reader_purpose``."""
    reg = registry(eng)
    reader = reader_purpose or PURPOSE_ANY
    viewer = Viewer(tenant=record.namespace, principal=record.namespace, kind=ViewerKind.OWNER)
    purposes = _purposes(eng, record)
    if purposes is None or not purposes:
        d = AdmissionDecision(
            allowed=False, reason=Reason.PURPOSE_TOO_BROAD, consent_generation=reg.generation
        )
        _emit(record.record_id, d, "read")
        return d
    last: AdmissionDecision | None = None
    for p in purposes:
        d = may_read(record_meta(record, p), reg, reader, viewer)
        if d.allowed:
            _emit(record.record_id, d, "read")
            return d
        last = d
    assert last is not None
    _emit(record.record_id, last, "read")
    return last


def store_denial(eng: Any, record: MemoryRecord) -> str | None:
    """``may_store`` at the write door: the denial reason code, None when the write may be stored."""
    reg = registry(eng)
    purposes = _purposes(eng, record) or [PURPOSE_ANY]
    last: AdmissionDecision | None = None
    for p in purposes:
        d = may_store(record_meta(record, p), reg, p)
        if d.allowed:
            _emit(record.record_id, d, "store")
            return None
        last = d
    assert last is not None
    _emit(record.record_id, last, "store")
    return last.reason.value


def derive_denial(eng: Any, record: MemoryRecord) -> str | None:
    """``may_derive`` for the subjects a derived record is about (entity, or its subject axis)."""
    reg = registry(eng)
    if not reg.do_not_derive:
        return None
    subjects = [s for s in (record.entity, *record.axes.subject) if s]
    for subject in subjects:
        d = may_derive(reg, record.namespace, record.namespace, subject)
        if not d.allowed:
            _emit(record.record_id, d, "derive")
            return d.reason.value
    return None
