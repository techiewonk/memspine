# ruff: noqa: E501
"""Source envelope intake, receipt and readiness, write ids and re-ingest (LIB-30; W-01, W-02, W-03, W-24).

Pure and unwired (wiring is W2-01): no I/O, no clock reads (``now`` is passed in), no model calls. The
book keeps its state in an injected :class:`IntakeStore`; :class:`InMemoryIntakeStore` is the reference
implementation. Layering: ``ingest`` sits below ``runtime``, so the quota check is an injected callable
(``QuotaGate.check`` fits behind a one-line adapter) and is invoked once, after validation and before
anything is stored (session 9). A rejected envelope leaves no state and no receipt.

What the book promises:

* **Write id (S3-3).** ``hash(owner scope, source id, version, position, content)`` where position is the
  source time when the segment has one, else its ``seg_index``. Client keys (``caller_id``) are ignored.
  The same words in a later source count as a new write. Identical segments inside one envelope are
  told apart by their occurrence number.
* **Idempotency.** A segment whose write id is already known is a retry: no new event. If every segment
  is a retry the original receipt is returned unchanged (``IntakeOutcome.replayed``).
* **Documents (W-24).** A document carries a version. Same version and same content is a retry; a
  different content under a known version is a conflict; a lower unknown version is stale. New content
  gives a chunk diff: chunks whose content already is live are kept, the rest are accepted, live chunks
  that disappeared are listed for retraction in :class:`VersionPlan`. Unchanged content under a new
  version is a no-op that returns the latest receipt.
* **Receipt (S7).** ``durability`` is ``durable``, or ``memory`` for an ephemeral book. Readiness starts
  ``pending`` for every configured capability and ``unavailable`` for the others; a capability that is
  not configured is never claimed. :meth:`IntakeBook.readiness` applies progress monotonically.
* **Events are returned, never written.** ``IntakeOutcome.events`` holds ``MemoryEvent`` drafts.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from memspine.observability import tap
from memspine.schemas.durable import MemoryEvent, OwnerScope, Provenance
from memspine.schemas.public import QuotaExceeded, Readiness, Receipt, WaitResult
from memspine.schemas.public.receipt import CAPABILITIES, ReadinessState
from memspine.schemas.steps.shared import Scope, Segment, SourceEnvelope

__all__ = [
    "InMemoryIntakeStore",
    "IntakeBook",
    "IntakeChange",
    "IntakeConflictError",
    "IntakeCost",
    "IntakeError",
    "IntakeLimits",
    "IntakeOutcome",
    "IntakeStore",
    "LiveSegment",
    "SourceState",
    "VersionPlan",
    "VersionRecord",
    "normalise_text",
    "owner_scope",
    "scope_key",
    "validate_envelope",
    "write_id",
]


class IntakeError(ValueError):
    """A malformed envelope or an invalid version/readiness request. Nothing was stored."""

    def __init__(self, message: str, *, seg_index: int | None = None) -> None:
        super().__init__(message if seg_index is None else f"segment {seg_index}: {message}")
        self.seg_index = seg_index


class IntakeConflictError(IntakeError):
    """The same (source, version) arrived with different content."""


@dataclass(frozen=True)
class IntakeLimits:
    max_segments: int = 1000
    max_segment_chars: int = 200_000
    max_envelope_chars: int = 2_000_000
    max_assets_per_segment: int = 16
    max_id_chars: int = 256


@dataclass(frozen=True)
class IntakeCost:
    """What an envelope would add; handed to the injected quota check."""

    scope_key: str
    storage_add: int  # bytes of new content (UTF-8)
    queue_add: int  # derivation jobs this acceptance enqueues
    segments_add: int


QuotaCheck = Callable[[IntakeCost], "QuotaExceeded | None"]

# ---------------------------------------------------------------------------------------------------
# Normalisation and validation
# ---------------------------------------------------------------------------------------------------
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def normalise_text(text: str) -> str:
    """NFC, CRLF/CR to LF, control characters (except tab and newline) dropped."""
    t = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    return _CTRL.sub("", t)


def _norm_id(value: str, what: str, limits: IntakeLimits, seg: int | None = None) -> str:
    v = unicodedata.normalize("NFC", value).strip()
    if not v:
        raise IntakeError(f"{what} must not be empty", seg_index=seg)
    if len(v) > limits.max_id_chars:
        raise IntakeError(f"{what} longer than {limits.max_id_chars} characters", seg_index=seg)
    return v


def validate_envelope(env: SourceEnvelope, limits: IntakeLimits | None = None) -> SourceEnvelope:
    """Return a normalised copy, or raise :class:`IntakeError` naming the segment. Writes nothing."""
    lim = limits or IntakeLimits()
    source_id = _norm_id(env.source_id, "source_id", lim)
    if not env.scope.tenant or not env.scope.user:
        raise IntakeError("scope needs a tenant and a user")
    if not env.segments:
        raise IntakeError("an envelope needs at least one segment")
    if len(env.segments) > lim.max_segments:
        raise IntakeError(f"more than {lim.max_segments} segments")
    if env.version is not None and env.version < 1:
        raise IntakeError("version must be >= 1")
    if env.kind != "document" and env.version not in (None, 1):
        raise IntakeError("only documents carry a version above 1")
    total = 0
    out: list[Segment] = []
    for i, seg in enumerate(env.segments):
        if seg.seg_index != i:
            raise IntakeError(
                f"seg_index must be dense from 0 (expected {i})", seg_index=seg.seg_index
            )
        content = normalise_text(seg.content)
        has_assets = bool(seg.assets)
        if seg.assets and len(seg.assets) > lim.max_assets_per_segment:
            raise IntakeError("too many assets", seg_index=i)
        if env.kind == "asset" and not has_assets:
            raise IntakeError("an asset segment needs an asset reference", seg_index=i)
        if not content.strip() and not (env.kind == "asset" and has_assets):
            raise IntakeError("content must not be blank", seg_index=i)
        if len(content) > lim.max_segment_chars:
            raise IntakeError(
                f"content longer than {lim.max_segment_chars} characters", seg_index=i
            )
        total += len(content)
        if total > lim.max_envelope_chars:
            raise IntakeError(f"envelope longer than {lim.max_envelope_chars} characters")
        if env.kind == "document" and seg.role != "document":
            raise IntakeError("document segments take role 'document'", seg_index=i)
        if env.kind == "conversation" and seg.role == "document":
            raise IntakeError("conversation segments cannot take role 'document'", seg_index=i)
        if env.kind == "tool_log" and seg.role != "tool":
            raise IntakeError("tool_log segments take role 'tool'", seg_index=i)
        caller_id = None
        if seg.caller_id is not None:
            caller_id = _norm_id(seg.caller_id, "caller_id", lim, i)
        out.append(seg.model_copy(update={"content": content, "caller_id": caller_id}))
    participants = None
    if env.participants is not None:
        participants = sorted({_norm_id(p, "participant", lim) for p in env.participants})
    return env.model_copy(
        update={"source_id": source_id, "segments": out, "participants": participants}
    )


# ---------------------------------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------------------------------
def _digest(*parts: Any) -> str:
    blob = json.dumps(parts, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


def owner_scope(scope: Scope) -> OwnerScope:
    if not scope.tenant or not scope.user:
        raise IntakeError("scope needs a tenant and a user")
    return OwnerScope(tenant=scope.tenant, owner=scope.user)


def scope_key(scope: Scope) -> str:
    """Identity scope: tenant plus owner only; agent and session are provenance (S3-1)."""
    o = owner_scope(scope)
    return _digest("scope", o.tenant, o.owner)


def _position(seg: Segment) -> str:
    at = seg.source_at
    if at is None:
        return f"i{seg.seg_index}"
    if at.tzinfo is not None:
        at = at.astimezone(UTC)
    return "t" + at.isoformat()


def _asset_hashes(seg: Segment) -> list[str]:
    return sorted(a.hash for a in (seg.assets or []))


def write_id(skey: str, source_id: str, version: int, seg: Segment, occurrence: int = 0) -> str:
    """S3-3 write id. Client keys are not an input."""
    return "w_" + _digest(
        "write",
        skey,
        source_id,
        version,
        _position(seg),
        seg.content,
        _asset_hashes(seg),
        occurrence,
    )


def _content_fp(seg: Segment) -> str:
    return _digest("content", seg.content, _asset_hashes(seg))


# ---------------------------------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class LiveSegment:
    seg_index: int
    write_id: str
    event_id: str
    record_id: str
    content_fp: str
    version: int
    intent_id: str


@dataclass(frozen=True)
class VersionRecord:
    version: int
    content_fp: str
    intent_id: str


@dataclass(frozen=True)
class SourceState:
    source_id: str
    kind: str
    latest_version: int
    versions: Mapping[int, VersionRecord]
    live: tuple[LiveSegment, ...]


@dataclass(frozen=True)
class IntakeChange:
    """One atomic state change, applied only after the quota check passed."""

    scope_key: str
    source_id: str
    kind: str
    version: int
    version_record: VersionRecord
    live: tuple[LiveSegment, ...]  # the full live set after the change
    new_write_ids: tuple[str, ...]
    receipt: Receipt
    readiness: Readiness


class IntakeStore(Protocol):
    def get_source(self, scope_key: str, source_id: str) -> SourceState | None: ...
    def get_write(self, scope_key: str, write_id: str) -> LiveSegment | None: ...
    def get_receipt(self, intent_id: str) -> Receipt | None: ...
    def get_readiness(self, intent_id: str) -> Readiness | None: ...
    def set_readiness(self, intent_id: str, readiness: Readiness) -> None: ...
    def commit(self, change: IntakeChange) -> None: ...


class InMemoryIntakeStore:
    def __init__(self) -> None:
        self._sources: dict[tuple[str, str], SourceState] = {}
        self._writes: dict[tuple[str, str], LiveSegment] = {}
        self._receipts: dict[str, Receipt] = {}
        self._readiness: dict[str, Readiness] = {}

    def get_source(self, scope_key: str, source_id: str) -> SourceState | None:
        return self._sources.get((scope_key, source_id))

    def get_write(self, scope_key: str, write_id: str) -> LiveSegment | None:
        return self._writes.get((scope_key, write_id))

    def get_receipt(self, intent_id: str) -> Receipt | None:
        return self._receipts.get(intent_id)

    def get_readiness(self, intent_id: str) -> Readiness | None:
        return self._readiness.get(intent_id)

    def set_readiness(self, intent_id: str, readiness: Readiness) -> None:
        self._readiness[intent_id] = readiness

    def commit(self, change: IntakeChange) -> None:
        key = (change.scope_key, change.source_id)
        prev = self._sources.get(key)
        versions = dict(prev.versions) if prev else {}
        versions[change.version] = change.version_record
        self._sources[key] = SourceState(
            change.source_id,
            change.kind,
            max(change.version, prev.latest_version if prev else 0),
            versions,
            change.live,
        )
        for seg in change.live:
            if seg.write_id in change.new_write_ids:
                self._writes[(change.scope_key, seg.write_id)] = seg
        self._receipts[change.receipt.intent_id] = change.receipt
        self._readiness[change.receipt.intent_id] = change.readiness


@dataclass(frozen=True)
class VersionPlan:
    """Supersede-the-source plan for a new document version."""

    source_id: str
    from_version: int
    to_version: int
    retract_event_ids: tuple[str, ...]
    accept_seg_indexes: tuple[int, ...]
    kept: tuple[tuple[int, str], ...]  # (new seg_index, existing event id) for unchanged chunks


@dataclass(frozen=True)
class IntakeOutcome:
    receipt: Receipt
    events: tuple[MemoryEvent, ...] = ()
    plan: VersionPlan | None = None
    replayed: bool = False  # every segment was a retry: the original receipt, no new events
    noop: bool = False  # unchanged document content under a new version: nothing to do


# ---------------------------------------------------------------------------------------------------
# The book
# ---------------------------------------------------------------------------------------------------
def _configured(capabilities: Iterable[str] | Mapping[str, bool]) -> frozenset[str]:
    names = (
        [k for k, v in capabilities.items() if v]
        if isinstance(capabilities, Mapping)
        else list(capabilities)
    )
    bad = [c for c in names if c not in CAPABILITIES]
    if bad:
        raise IntakeError(f"unknown capability {bad[0]!r}")
    return frozenset(names)


def _initial_readiness(configured: frozenset[str]) -> Readiness:
    return Readiness(**{c: ("pending" if c in configured else "unavailable") for c in CAPABILITIES})


class IntakeBook:
    """Accepts envelopes, returns receipts and event drafts, tracks readiness. Pure over its store."""

    def __init__(
        self,
        store: IntakeStore | None = None,
        *,
        ephemeral: bool = False,
        limits: IntakeLimits | None = None,
    ) -> None:
        self.store: IntakeStore = store if store is not None else InMemoryIntakeStore()
        self.ephemeral = ephemeral
        self.limits = limits or IntakeLimits()

    # -- public -------------------------------------------------------------------------------------
    def accept(
        self,
        envelope: SourceEnvelope,
        capabilities: Collection[str] | Mapping[str, bool],
        quota_check: QuotaCheck | None,
        *,
        now: datetime,
    ) -> IntakeOutcome | QuotaExceeded:
        """Validate, check quota (once, before any state change), then accept.

        Returns ``QuotaExceeded`` (no receipt, no state) or an :class:`IntakeOutcome` whose ``receipt``
        is the answer for the caller and whose ``events`` are drafts for the event log.
        """
        if now.tzinfo is None:
            raise IntakeError("now must be timezone-aware")
        env = validate_envelope(envelope, self.limits)
        configured = _configured(capabilities)
        skey = scope_key(env.scope)
        if env.kind == "document":
            out = self._document(env, skey, configured, quota_check, now)
        else:
            out = self._stream(env, skey, configured, quota_check, now)
        if tap.enabled():
            if isinstance(out, QuotaExceeded):
                tap.event("write.intake.rejected", reason="quota", kind=env.kind)
            else:
                tap.event(
                    "write.intake.received",
                    kind=env.kind,
                    source_id=env.source_id,
                    segments=len(env.segments),
                    version=out.receipt.source_version,
                    replayed=out.replayed,
                    noop=out.noop,
                    new_events=len(out.events),
                    intent_id=out.receipt.intent_id,
                )
        return out

    def readiness(
        self, intent_id: str, progress: Mapping[str, ReadinessState] | None = None
    ) -> Readiness:
        """Current readiness of a receipt; ``progress`` updates capabilities (monotonic).

        ``unavailable`` and ``ready`` are final; ``failed`` may go back to ``pending`` or on to ``ready``
        (a retried derivation); ``pending`` moves to anything configured.
        """
        cur = self.store.get_readiness(intent_id)
        if cur is None:
            raise KeyError(intent_id)
        if not progress:
            return cur
        vals = {c: cur.get(c) for c in CAPABILITIES}
        for cap, new in progress.items():
            if cap not in CAPABILITIES:
                raise IntakeError(f"unknown capability {cap!r}")
            old = vals[cap]
            if new == old:
                continue
            if old == "unavailable":
                raise IntakeError(f"{cap} is not configured and cannot become {new}")
            if old == "ready":
                raise IntakeError(f"{cap} is already ready")
            if new == "unavailable":
                raise IntakeError(f"{cap} cannot become unavailable once configured")
            vals[cap] = new
        updated = Readiness(**vals)
        self.store.set_readiness(intent_id, updated)
        return updated

    def wait_snapshot(
        self, intent_id: str, requested: Sequence[str], *, waited_ms: int = 0
    ) -> WaitResult:
        """The readiness reached so far for ``requested``; ``timed_out`` iff any is still pending."""
        r = self.readiness(intent_id)
        req = list(dict.fromkeys(requested))
        return WaitResult(
            requested=req,  # type: ignore[arg-type]
            readiness=r,
            timed_out=bool(r.pending(req)),
            waited_ms=waited_ms,
        )

    def receipt(self, intent_id: str) -> Receipt | None:
        return self.store.get_receipt(intent_id)

    # -- internals ----------------------------------------------------------------------------------
    def _durability(self) -> Any:
        return "memory" if self.ephemeral else "durable"

    def _ids(self, env: SourceEnvelope, skey: str, version: int) -> list[str]:
        seen: dict[str, int] = {}
        ids: list[str] = []
        for seg in env.segments:
            base = write_id(skey, env.source_id, version, seg, 0)
            n = seen.get(base, 0)
            seen[base] = n + 1
            ids.append(base if n == 0 else write_id(skey, env.source_id, version, seg, n))
        return ids

    def _gate(
        self, quota_check: QuotaCheck | None, skey: str, segs: Sequence[Segment]
    ) -> QuotaExceeded | None:
        if quota_check is None:
            return None
        return quota_check(
            IntakeCost(
                scope_key=skey,
                storage_add=sum(len(s.content.encode("utf-8")) for s in segs),
                queue_add=1,
                segments_add=len(segs),
            )
        )

    def _provenance(self, env: SourceEnvelope, version: int) -> Provenance:
        return Provenance(
            session_id=env.scope.session,
            agent_id=env.scope.agent,
            principal=env.scope.principal,
            source_id=env.source_id,
            source_version=version,
        )

    def _draft(
        self, env: SourceEnvelope, seg: Segment, wid: str, version: int, now: datetime
    ) -> MemoryEvent:
        payload: dict[str, Any] = {
            "write_id": wid,
            "idem_key": wid,
            "record_id": "rec-" + wid,
            "source_id": env.source_id,
            "source_kind": env.kind,
            "source_version": version,
            "seg_index": seg.seg_index,
            "role": seg.role,
            "content": seg.content,
            "content_fp": _content_fp(seg),
            "source_at": seg.source_at.isoformat() if seg.source_at else None,
            "tz": seg.tz,
            "envelope_tz": env.tz,
            "speaker": seg.speaker,
            "addressee": seg.addressee,
            "participants": env.participants,
            "reply_to": seg.reply_to,
            "visibility": seg.visibility,
            "purposes": seg.purposes,
            "assets": [a.model_dump() for a in (seg.assets or [])],
        }
        return MemoryEvent(
            event_id="evt-" + wid,
            kind="memory.write",
            scope=owner_scope(env.scope),
            provenance=self._provenance(env, version),
            ts=now,
            actor=env.scope.principal or "system",
            payload=payload,
            fingerprint=_content_fp(seg),
        )

    def _receipt(
        self,
        env: SourceEnvelope,
        configured: frozenset[str],
        intent_id: str,
        version: int,
        order: Sequence[LiveSegment],
        retried: list[int],
    ) -> tuple[Receipt, Readiness]:
        ready = _initial_readiness(configured)
        return (
            Receipt(
                durability=self._durability(),
                event_ids=[s.event_id for s in order],
                record_ids=[s.record_id for s in order],
                intent_id=intent_id,
                source_id=env.source_id,
                source_version=version,
                readiness=ready,
                retried=retried,
            ),
            ready,
        )

    def _stream(
        self,
        env: SourceEnvelope,
        skey: str,
        configured: frozenset[str],
        quota_check: QuotaCheck | None,
        now: datetime,
    ) -> IntakeOutcome | QuotaExceeded:
        wids = self._ids(env, skey, 1)
        known = [self.store.get_write(skey, w) for w in wids]
        fresh = [i for i, k in enumerate(known) if k is None]
        if not fresh:
            return self._replay([k for k in known if k is not None], env)
        rejected = self._gate(quota_check, skey, [env.segments[i] for i in fresh])
        if rejected is not None:
            return rejected
        intent_id = "intent-" + _digest("intent", skey, env.source_id, 1, [wids[i] for i in fresh])
        state = self.store.get_source(skey, env.source_id)
        events: list[MemoryEvent] = []
        order: list[LiveSegment] = []
        added: list[LiveSegment] = []
        for i, seg in enumerate(env.segments):
            existing = known[i]
            if existing is not None:
                order.append(existing)
                continue
            live = LiveSegment(
                seg.seg_index,
                wids[i],
                "evt-" + wids[i],
                "rec-" + wids[i],
                _content_fp(seg),
                1,
                intent_id,
            )
            events.append(self._draft(env, seg, wids[i], 1, now))
            order.append(live)
            added.append(live)
        retried = [i for i, k in enumerate(known) if k is not None]
        receipt, ready = self._receipt(env, configured, intent_id, 1, order, retried)
        prev_live = state.live if state else ()
        vrec = VersionRecord(
            1, _digest("source", [s.content_fp for s in (*prev_live, *added)]), intent_id
        )
        self.store.commit(
            IntakeChange(
                skey,
                env.source_id,
                env.kind,
                1,
                vrec,
                (*prev_live, *added),
                tuple(s.write_id for s in added),
                receipt,
                ready,
            )
        )
        return IntakeOutcome(receipt=receipt, events=tuple(events))

    def _replay(self, known: Sequence[LiveSegment], env: SourceEnvelope) -> IntakeOutcome:
        origins = {k.intent_id for k in known}
        if len(origins) == 1:
            original = self.store.get_receipt(next(iter(origins)))
            if original is not None and original.event_ids == [k.event_id for k in known]:
                return IntakeOutcome(receipt=original, replayed=True)
        base = self.store.get_receipt(known[-1].intent_id)
        if base is None:  # a store that lost a receipt must not guess
            raise IntakeError("receipt missing for a known write")
        receipt = base.model_copy(
            update={
                "event_ids": [k.event_id for k in known],
                "record_ids": [k.record_id for k in known],
                "retried": list(range(len(known))),
                "source_id": env.source_id,
            }
        )
        return IntakeOutcome(receipt=receipt, replayed=True)

    def _original(self, rec: VersionRecord) -> Receipt:
        original = self.store.get_receipt(rec.intent_id)
        if original is None:
            raise IntakeError("receipt missing for a known version")
        return original

    def _document(
        self,
        env: SourceEnvelope,
        skey: str,
        configured: frozenset[str],
        quota_check: QuotaCheck | None,
        now: datetime,
    ) -> IntakeOutcome | QuotaExceeded:
        state = self.store.get_source(skey, env.source_id)
        fp = _digest("source", [_content_fp(s) for s in env.segments])
        latest = state.latest_version if state else 0
        version = env.version
        if state is not None:
            if version is not None and version in state.versions:
                rec = state.versions[version]
                if rec.content_fp != fp:
                    raise IntakeConflictError(
                        f"version {version} of {env.source_id!r} already has different content"
                    )
                return IntakeOutcome(receipt=self._original(rec), replayed=True)
            if version is not None and version < latest:
                raise IntakeError(f"stale version {version}; latest is {latest}")
            if state.versions[latest].content_fp == fp:
                return IntakeOutcome(
                    receipt=self._original(state.versions[latest]), replayed=True, noop=True
                )
        version = version if version is not None else latest + 1

        old_live = list(state.live) if state else []
        pool: dict[str, list[LiveSegment]] = {}
        for o in old_live:
            pool.setdefault(o.content_fp, []).append(o)
        wids = self._ids(env, skey, version)
        kept: dict[int, LiveSegment] = {}
        for i, seg in enumerate(env.segments):
            bucket = pool.get(_content_fp(seg))
            if bucket:
                kept[i] = bucket.pop(0)
        fresh = [i for i in range(len(env.segments)) if i not in kept]
        kept_ids = {k.event_id for k in kept.values()}
        retract = tuple(o.event_id for o in old_live if o.event_id not in kept_ids)
        if fresh:
            rejected = self._gate(quota_check, skey, [env.segments[i] for i in fresh])
            if rejected is not None:
                return rejected
        intent_id = "intent-" + _digest(
            "intent", skey, env.source_id, version, [wids[i] for i in fresh], list(retract)
        )
        events: list[MemoryEvent] = []
        order: list[LiveSegment] = []
        for i, seg in enumerate(env.segments):
            k = kept.get(i)
            if k is not None:
                order.append(
                    LiveSegment(
                        i, k.write_id, k.event_id, k.record_id, k.content_fp, k.version, k.intent_id
                    )
                )
                continue
            order.append(
                LiveSegment(
                    i,
                    wids[i],
                    "evt-" + wids[i],
                    "rec-" + wids[i],
                    _content_fp(seg),
                    version,
                    intent_id,
                )
            )
            events.append(self._draft(env, seg, wids[i], version, now))
        if state is not None:
            events.append(
                MemoryEvent(
                    event_id="evt-" + _digest("link", skey, env.source_id, version),
                    kind="memory.link",
                    scope=owner_scope(env.scope),
                    provenance=self._provenance(env, version),
                    ts=now,
                    actor=env.scope.principal or "system",
                    payload={
                        "relation": "version_of",
                        "source_id": env.source_id,
                        "from_version": latest,
                        "to_version": version,
                        "retract_event_ids": list(retract),
                    },
                )
            )
        receipt, ready = self._receipt(env, configured, intent_id, version, order, [])
        self.store.commit(
            IntakeChange(
                skey,
                env.source_id,
                env.kind,
                version,
                VersionRecord(version, fp, intent_id),
                tuple(order),
                tuple(order[i].write_id for i in fresh),
                receipt,
                ready,
            )
        )
        plan = (
            VersionPlan(
                env.source_id,
                latest,
                version,
                retract,
                tuple(fresh),
                tuple((i, kept[i].event_id) for i in sorted(kept)),
            )
            if state is not None
            else None
        )
        return IntakeOutcome(receipt=receipt, events=tuple(events), plan=plan)
