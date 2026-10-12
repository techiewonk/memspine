# ruff: noqa: E501
"""Retention classes, retention plans and the hash-chained audit log (LIB-41; G-14, G-15, ER-27 C, ER-30).

Pure and unwired: no I/O, no clock reads (``now`` is passed in), no model calls.

* Retention classes: a match expression plus an ISO-8601 TTL. ``classify`` picks one class per record.
* ``retention_plan``: which records expire, what remains (tombstones and lineage), the new
  ``RetentionBoundary`` and the rebuild capability to advertise (full / limited / unavailable). The event
  log (layer L0) under ``full`` never expires; under ``rolling`` it is pruned by a window; under
  ``ephemeral`` it is not kept.
* Audit: ``AuditRecord`` carries who, which action, which ids, purpose, when and outcome, never content.
  Records are hash-chained (``prev_hash``); ``verify_chain`` detects tampering, reordering and gaps.

Match expression grammar (this module defines it; the schema leaves ``RetentionClass.match`` open)::

    match  := "*" | clause ("&" clause)*
    clause := field op value ("|" value)*
    field  := kind | layer | tier | purpose
    op     := "=" | "!="            (tier also: ">=" "<=" ">" "<")

``purpose=a`` matches a record whose purpose is ``a`` or a sub-purpose (``a:b``), as in consent.
Tier values are names: public, personal, sensitive, special_category.
TTL: an ISO-8601 duration (``P30D``, ``P1Y6M``, ``PT12H``, ``P2W``) or ``never``.
"""

from __future__ import annotations

import calendar
import hashlib
import json
import re
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.erasure import RetentionBoundary
from memspine.schemas.steps.offline import RetentionClass
from memspine.schemas.steps.shared import Layer
from memspine.trust.consent import Tier, purpose_covers

__all__ = [
    "AUDIT_ACTIONS",
    "DEFAULT_CLASS",
    "GENESIS_HASH",
    "AuditAction",
    "AuditEntry",
    "AuditLog",
    "AuditOutcome",
    "AuditRecord",
    "ChainReport",
    "Duration",
    "PlanHold",
    "PlannedExpiry",
    "RetentionFormatError",
    "RetentionPlan",
    "RetentionProfile",
    "RetentionRecord",
    "RetentionRule",
    "audit_admin",
    "audit_consent",
    "audit_erase",
    "audit_read",
    "audit_retention_expiry",
    "audit_write",
    "classify",
    "default_profile",
    "must_audit",
    "parse_duration",
    "parse_ttl",
    "profile_names",
    "prune_audit",
    "record_audit",
    "retention_plan",
    "verify_chain",
]


class RetentionFormatError(ValueError):
    """A match expression or a TTL is malformed."""


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


def _aware(dt: datetime, what: str) -> datetime:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware")
    return dt.astimezone(UTC)


# ------------------------------------------------------------------------------ durations


_DURATION_RE = re.compile(
    r"^P(?:(?P<y>\d+)Y)?(?:(?P<mo>\d+)M)?(?:(?P<w>\d+)W)?(?:(?P<d>\d+)D)?"
    r"(?:T(?:(?P<h>\d+)H)?(?:(?P<mi>\d+)M)?(?:(?P<s>\d+)S)?)?$"
)


class Duration(_Frozen):
    """A calendar-aware ISO-8601 duration (whole numbers only; no negatives, no fractions)."""

    years: int = Field(default=0, ge=0)
    months: int = Field(default=0, ge=0)
    days: int = Field(default=0, ge=0)  # weeks are folded in at parse time
    seconds: int = Field(default=0, ge=0)  # hours and minutes are folded in at parse time

    @property
    def is_zero(self) -> bool:
        return not (self.years or self.months or self.days or self.seconds)

    def _shift(self, dt: datetime, sign: int) -> datetime:
        dt = _aware(dt, "datetime")
        total_months = dt.year * 12 + (dt.month - 1) + sign * (self.years * 12 + self.months)
        year, month = divmod(total_months, 12)
        month += 1
        if not 1 <= year <= 9999:
            raise OverflowError("date out of range")
        day = min(dt.day, calendar.monthrange(year, month)[1])
        base = dt.replace(year=year, month=month, day=day)
        return base + sign * timedelta(days=self.days, seconds=self.seconds)

    def add_to(self, dt: datetime) -> datetime:
        """``dt`` plus this duration (months and years by the calendar, day clamped to month end)."""
        return self._shift(dt, 1)

    def subtract_from(self, dt: datetime) -> datetime:
        return self._shift(dt, -1)


def parse_duration(text: str) -> Duration:
    """Parse an ISO-8601 duration without third-party libraries."""
    if not isinstance(text, str):
        raise RetentionFormatError(f"duration must be a string, got {type(text).__name__}")
    s = text.strip().upper()
    m = _DURATION_RE.match(s)
    if m is None or s == "P" or s.endswith("T"):
        raise RetentionFormatError(f"not an ISO-8601 duration: {text!r}")
    g = {k: int(v) if v else 0 for k, v in m.groupdict().items()}
    return Duration(
        years=g["y"],
        months=g["mo"],
        days=g["d"] + 7 * g["w"],
        seconds=g["h"] * 3600 + g["mi"] * 60 + g["s"],
    )


_NEVER = frozenset({"never", "forever", "none"})


def parse_ttl(text: str | None) -> Duration | None:
    """A TTL: an ISO duration, or ``never`` / ``None`` for no expiry."""
    if text is None or text.strip().lower() in _NEVER:
        return None
    return parse_duration(text)


# ------------------------------------------------------------------------------ match rules


_TIER_BY_NAME = {t.name.lower(): t for t in Tier}
_LAYERS = ("L0", "L1", "L2", "L3", "L4", "L5")
_CLAUSE_RE = re.compile(r"^\s*(kind|layer|tier|purpose)\s*(>=|<=|!=|=|>|<)\s*(\S.*?)\s*$")


class _Clause(_Frozen):
    field: Literal["kind", "layer", "tier", "purpose"]
    op: Literal["=", "!=", ">=", "<=", ">", "<"]
    values: tuple[str, ...]

    def test(self, rec: RetentionRecord) -> bool:
        if self.field == "tier":
            tiers = [_TIER_BY_NAME[v] for v in self.values]
            t = rec.tier
            return {
                "=": t in tiers,
                "!=": t not in tiers,
                ">=": t >= tiers[0],
                "<=": t <= tiers[0],
                ">": t > tiers[0],
                "<": t < tiers[0],
            }[self.op]
        if self.field == "purpose":
            hit = any(purpose_covers(v, rec.purpose) for v in self.values)
        elif self.field == "layer":
            hit = rec.layer in self.values
        else:
            hit = rec.kind in self.values
        return hit if self.op == "=" else not hit


def _compile_match(expr: str) -> tuple[_Clause, ...]:
    if not isinstance(expr, str) or not expr.strip():
        raise RetentionFormatError("empty match expression")
    if expr.strip() == "*":
        return ()
    out: list[_Clause] = []
    for part in expr.split("&"):
        m = _CLAUSE_RE.match(part)
        if m is None:
            raise RetentionFormatError(f"bad match clause: {part.strip()!r}")
        field, op, raw = m.groups()
        values = tuple(v.strip() for v in raw.split("|"))
        if any(not v for v in values):
            raise RetentionFormatError(f"empty value in clause: {part.strip()!r}")
        if field != "tier" and op not in ("=", "!="):
            raise RetentionFormatError(f"operator {op!r} is only valid for tier")
        if field == "tier":
            unknown = [v for v in values if v.lower() not in _TIER_BY_NAME]
            if unknown:
                raise RetentionFormatError(f"unknown tier {unknown[0]!r}")
            values = tuple(v.lower() for v in values)
            if op in (">=", "<=", ">", "<") and len(values) != 1:
                raise RetentionFormatError("an ordered tier comparison takes one value")
        if field == "layer":
            unknown = [v for v in values if v not in _LAYERS]
            if unknown:
                raise RetentionFormatError(f"unknown layer {unknown[0]!r}")
        out.append(_Clause(field=field, op=op, values=values))  # type: ignore[arg-type]
    return tuple(out)


class RetentionRecord(_Frozen):
    """What retention needs to know about one record, projection or event (never its content)."""

    id: str = Field(min_length=1)
    kind: str = "semantic"
    layer: Layer = "L1"
    tier: Tier = Tier.PERSONAL
    purpose: str = "*"
    created_at: datetime
    seq: int | None = Field(default=None, ge=0)  # event-log position, when the record is an event
    legal_hold: bool = False
    pending: bool = False  # pending derivation work depends on it; pruning is blocked (ER-27)

    @field_validator("created_at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "created_at")


class RetentionRule(_Frozen):
    """One retention class: a match rule and a TTL (``None`` = never expires)."""

    name: str = Field(min_length=1)
    match: str = "*"
    ttl: str | None = None
    priority: int = 0
    applies_to_layers: tuple[Layer, ...] = ()
    hold: bool = False  # a class that holds its members (never expire, whatever the ttl)

    @model_validator(mode="after")
    def _valid(self) -> RetentionRule:
        _compile_match(self.match)
        parse_ttl(self.ttl)
        return self

    @property
    def duration(self) -> Duration | None:
        return None if self.hold else parse_ttl(self.ttl)

    @property
    def specificity(self) -> int:
        return len(_compile_match(self.match)) + (1 if self.applies_to_layers else 0)

    def matches(self, rec: RetentionRecord) -> bool:
        if self.applies_to_layers and rec.layer not in self.applies_to_layers:
            return False
        return all(c.test(rec) for c in _compile_match(self.match))

    @classmethod
    def from_schema(cls, rc: RetentionClass, *, name: str, priority: int = 0) -> RetentionRule:
        """Adopt a ``memspine.schemas`` RetentionClass, whose match and ttl are plain strings."""
        return cls(
            name=name,
            match=rc.match,
            ttl=rc.ttl,
            priority=priority,
            applies_to_layers=tuple(rc.applies_to_layers),
        )


DEFAULT_CLASS = RetentionRule(name="default", match="*", ttl=None, priority=-(2**31))


def classify(record: RetentionRecord, classes: Iterable[RetentionRule]) -> RetentionRule:
    """The class that governs ``record``: highest priority, then most specific, then name.

    Falls back to ``DEFAULT_CLASS`` (never expires: personal long-term memory is never auto-expired).
    """
    best: RetentionRule | None = None
    best_key: tuple[int, int, str] | None = None
    for rule in classes:
        if not rule.matches(record):
            continue
        key = (-rule.priority, -rule.specificity, rule.name)
        if best_key is None or key < best_key:
            best, best_key = rule, key
    return best if best is not None else DEFAULT_CLASS


# ------------------------------------------------------------------------------ profiles


AuditAction = Literal["read", "write", "erase", "consent", "admin"]
AuditOutcome = Literal["ok", "denied", "failed"]
AUDIT_ACTIONS: tuple[str, ...] = ("read", "write", "erase", "consent", "admin")
_MODE_RANK = {"full": 0, "rolling": 1, "ephemeral": 2}


class RetentionProfile(_Frozen):
    """Retention and audit settings of one storage profile (G-14, G-15).

    ``store`` records the storage family (embedded sqlite or server postgres); it does not change
    behaviour here, the policy is the same on both.
    """

    name: str
    store: Literal["embedded", "server"] = "embedded"
    event_log_mode: Literal["full", "rolling", "ephemeral"] = "full"
    rolling_window: str | None = None  # ISO duration; required when rolling
    classes: tuple[RetentionRule, ...] = ()
    audit_reads: bool = False
    audit_writes: bool = False
    audit_retention: str | None = None  # ISO duration; None keeps the audit log forever

    @model_validator(mode="after")
    def _valid(self) -> RetentionProfile:
        if self.event_log_mode == "rolling":
            if self.rolling_window is None or parse_duration(self.rolling_window).is_zero:
                raise ValueError("rolling retention needs a non-zero rolling_window")
        elif self.rolling_window is not None:
            parse_duration(self.rolling_window)
        parse_ttl(self.audit_retention)
        names = [c.name for c in self.classes]
        if len(set(names)) != len(names):
            raise ValueError("retention class names must be unique within a profile")
        return self


_SCRATCH = "kind=working|session"


def _scratch(name: str, ttl: str, **kw: object) -> RetentionRule:
    return RetentionRule(name=name, match=_SCRATCH, ttl=ttl, **kw)  # type: ignore[arg-type]


def default_profile(
    name: str, store: Literal["embedded", "server"] = "embedded"
) -> RetentionProfile:
    """The G-14 / G-15 templates. TTL and audit-retention values are starting points, not decisions."""
    if name == "assistant":
        classes = (
            RetentionRule(name="working", match="kind=working", ttl="P7D"),
            RetentionRule(name="session", match="kind=session", ttl="P90D"),
        )
        return RetentionProfile(name=name, store=store, classes=classes, audit_retention="P1Y")
    if name == "regulated":
        classes = (
            _scratch("scratch-sensitive", "P30D").model_copy(
                update={"match": f"{_SCRATCH} & tier>=sensitive"}
            ),
            _scratch("scratch", "P90D"),
        )
        return RetentionProfile(
            name=name,
            store=store,
            classes=classes,
            audit_reads=True,
            audit_writes=True,
            audit_retention="P7Y",
        )
    if name == "coding":
        return RetentionProfile(
            name=name,
            store=store,
            event_log_mode="rolling",
            rolling_window="P90D",
            classes=(_scratch("scratch", "P14D"),),
            audit_retention="P90D",
        )
    if name == "multi_agent":
        return RetentionProfile(
            name=name,
            store=store,
            classes=(_scratch("scratch", "P30D"),),
            audit_reads=True,
            audit_retention="P2Y",
        )
    if name == "ephemeral":
        return RetentionProfile(
            name=name,
            store=store,
            event_log_mode="ephemeral",
            classes=(_scratch("scratch", "P1D"),),
            audit_retention="P7D",
        )
    raise KeyError(f"unknown profile {name!r}; known: {', '.join(profile_names())}")


def profile_names() -> tuple[str, ...]:
    return ("assistant", "coding", "ephemeral", "multi_agent", "regulated")


# ------------------------------------------------------------------------------ retention plan


class PlannedExpiry(_Frozen):
    record_id: str
    layer: Layer
    retention_class: str
    expires_at: datetime | None
    leaves_tombstone: bool = True  # lineage and the tombstone stay; only content and projections go


class PlanHold(_Frozen):
    record_id: str
    reason: Literal["legal_hold", "pending_work", "hold_class"]


class RetentionPlan(_Frozen):
    """What a retention pass would do. Ids only; applying it is someone else's job."""

    now: datetime
    profile: str
    expire: tuple[PlannedExpiry, ...] = ()
    held: tuple[PlanHold, ...] = ()
    retained: tuple[str, ...] = ()  # records that stay in full
    tombstones: tuple[str, ...] = ()  # ids that keep a tombstone after expiry
    lineage_kept: bool = True
    boundary: RetentionBoundary
    rebuild: Literal["full", "limited", "unavailable"]
    l0_protected: int = 0  # event-log records skipped because mode is full

    @property
    def expired_ids(self) -> tuple[str, ...]:
        return tuple(e.record_id for e in self.expire)

    @property
    def advertises_limited_rebuild(self) -> bool:
        return self.rebuild == "limited"


def _effective_mode(profile_mode: str, previous: RetentionBoundary | None) -> str:
    """Pruned history cannot be un-pruned: the weaker of the profile mode and the previous mode wins."""
    if previous is None:
        return profile_mode
    return max(profile_mode, previous.mode, key=_MODE_RANK.__getitem__)


def retention_plan(
    records: Sequence[RetentionRecord],
    now: datetime,
    profile: RetentionProfile,
    *,
    scope: OwnerScope,
    previous: RetentionBoundary | None = None,
) -> RetentionPlan:
    """Plan expiry for ``records`` under ``profile`` at ``now``.

    * Layer L0 (the event log): never expires under full; pruned by the rolling window under rolling;
      not kept under ephemeral. Other layers expire by their class TTL.
    * Legal-hold, pending-work and hold-class records are never expired.
    * Expired records leave a tombstone and keep their lineage.
    * The boundary only moves forward, and a log that was ever pruned never advertises a full rebuild.
    """
    now = _aware(now, "now")
    mode = _effective_mode(profile.event_log_mode, previous)
    window = parse_duration(profile.rolling_window) if profile.rolling_window else None
    cutoff = window.subtract_from(now) if (mode == "rolling" and window is not None) else None
    if (
        mode == "rolling"
        and previous is not None
        and previous.mode == "rolling"
        and previous.boundary_ts is not None
        and (cutoff is None or previous.boundary_ts > cutoff)
    ):
        cutoff = previous.boundary_ts
    # previous rolling boundary but profile now full: keep the old cutoff, no new pruning.
    prune_l0_by_cutoff = mode == "rolling" and profile.event_log_mode == "rolling"

    expire: list[PlannedExpiry] = []
    held: list[PlanHold] = []
    retained: list[str] = []
    l0_protected = 0
    pruned_seqs: list[int] = []

    for rec in sorted(records, key=lambda r: r.id):
        rule = classify(rec, profile.classes)
        if rec.legal_hold:
            held.append(PlanHold(record_id=rec.id, reason="legal_hold"))
            continue
        if rec.pending:
            held.append(PlanHold(record_id=rec.id, reason="pending_work"))
            continue
        if rec.layer == "L0":
            if mode == "full":
                l0_protected += 1
                retained.append(rec.id)
                continue
            if mode == "ephemeral":
                due, when = True, None
            else:
                due = prune_l0_by_cutoff and cutoff is not None and rec.created_at <= cutoff
                when = rec.created_at
            if due:
                expire.append(
                    PlannedExpiry(
                        record_id=rec.id, layer="L0", retention_class="event_log", expires_at=when
                    )
                )
                if rec.seq is not None:
                    pruned_seqs.append(rec.seq)
            else:
                retained.append(rec.id)
            continue
        if rule.hold:
            held.append(PlanHold(record_id=rec.id, reason="hold_class"))
            continue
        dur = rule.duration
        if dur is None:
            retained.append(rec.id)
            continue
        expires_at = dur.add_to(rec.created_at)
        if expires_at <= now:
            expire.append(
                PlannedExpiry(
                    record_id=rec.id,
                    layer=rec.layer,
                    retention_class=rule.name,
                    expires_at=expires_at,
                )
            )
        else:
            retained.append(rec.id)

    boundary = _boundary(scope, mode, now, cutoff, pruned_seqs, previous)
    return RetentionPlan(
        now=now,
        profile=profile.name,
        expire=tuple(expire),
        held=tuple(held),
        retained=tuple(retained),
        tombstones=tuple(e.record_id for e in expire),
        boundary=boundary,
        rebuild=boundary.rebuild,
        l0_protected=l0_protected,
    )


def _boundary(
    scope: OwnerScope,
    mode: str,
    now: datetime,
    cutoff: datetime | None,
    pruned_seqs: list[int],
    previous: RetentionBoundary | None,
) -> RetentionBoundary:
    if mode == "full":
        return RetentionBoundary(scope=scope, mode="full", rebuild="full")
    if mode == "ephemeral":
        return RetentionBoundary(scope=scope, mode="ephemeral", rebuild="unavailable")
    seq_candidates = list(pruned_seqs)
    if previous is not None and previous.boundary_seq is not None:
        seq_candidates.append(previous.boundary_seq)
    ts = cutoff if cutoff is not None else now
    return RetentionBoundary(
        scope=scope,
        mode="rolling",
        rebuild="limited",
        boundary_seq=max(seq_candidates) if seq_candidates else None,
        boundary_ts=ts,
    )


# ------------------------------------------------------------------------------ audit


GENESIS_HASH = "0" * 64
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/\-]{0,127}$")
_PURPOSE_RE = re.compile(r"^[A-Za-z0-9*][A-Za-z0-9._:*\-]{0,63}$")
_CODE_RE = re.compile(r"^[a-z0-9][a-z0-9_.:\-]{0,63}$")


class AuditEntry(_Frozen):
    """An audit event before sealing: who did what to which ids, for what purpose. No content."""

    actor: str
    action: AuditAction
    op: str  # short verb: search, assemble, remember, correct, forget, export, grant, revoke, ...
    ids: tuple[str, ...] = ()
    purpose: str = "*"
    at: datetime
    outcome: AuditOutcome = "ok"
    reason: str | None = None  # a short code, never free text
    tenant: str | None = None

    @field_validator("at")
    @classmethod
    def _tz(cls, v: datetime) -> datetime:
        return _aware(v, "at")

    @field_validator("actor", "tenant")
    @classmethod
    def _ident(cls, v: str | None) -> str | None:
        if v is not None and not _ID_RE.match(v):
            raise ValueError("actor and tenant are identifiers, not free text")
        return v

    @field_validator("op", "reason")
    @classmethod
    def _code(cls, v: str | None) -> str | None:
        if v is not None and not _CODE_RE.match(v):
            raise ValueError("op and reason are short lowercase codes, not free text")
        return v

    @field_validator("purpose")
    @classmethod
    def _purpose(cls, v: str) -> str:
        if not _PURPOSE_RE.match(v):
            raise ValueError("purpose is a purpose path, not free text")
        return v

    @field_validator("ids")
    @classmethod
    def _ids(cls, v: tuple[str, ...]) -> tuple[str, ...]:
        for i in v:
            if not _ID_RE.match(i):
                raise ValueError("ids must be identifiers; content is never audited")
        return v


class AuditRecord(AuditEntry):
    """A sealed audit event: sequence number, previous hash and its own hash."""

    seq: int = Field(ge=1)
    prev_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    def compute_hash(self) -> str:
        return _digest(self)


def _digest(rec: AuditEntry | AuditRecord) -> str:
    body = {
        "seq": getattr(rec, "seq", None),
        "prev_hash": getattr(rec, "prev_hash", None),
        "actor": rec.actor,
        "action": rec.action,
        "op": rec.op,
        "ids": list(rec.ids),
        "purpose": rec.purpose,
        "at": rec.at.astimezone(UTC).isoformat(),
        "outcome": rec.outcome,
        "reason": rec.reason,
        "tenant": rec.tenant,
    }
    blob = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(blob.encode("ascii")).hexdigest()


def _seal(entry: AuditEntry, seq: int, prev_hash: str) -> AuditRecord:
    draft = AuditRecord(**entry.model_dump(), seq=seq, prev_hash=prev_hash, hash=GENESIS_HASH)
    return draft.model_copy(update={"hash": draft.compute_hash()})


class AuditLog(_Frozen):
    """An append-only, hash-chained log (immutable value: ``append`` returns a new log).

    ``anchor_hash`` / ``anchor_seq`` are the hash and seq of the last pruned record (genesis when
    nothing was pruned), so a pruned log still verifies from its anchor.
    """

    records: tuple[AuditRecord, ...] = ()
    anchor_hash: str = GENESIS_HASH
    anchor_seq: int = 0

    @property
    def head_hash(self) -> str:
        return self.records[-1].hash if self.records else self.anchor_hash

    @property
    def next_seq(self) -> int:
        return self.records[-1].seq + 1 if self.records else self.anchor_seq + 1

    def append(self, entry: AuditEntry) -> AuditLog:
        rec = _seal(entry, self.next_seq, self.head_hash)
        return self.model_copy(update={"records": (*self.records, rec)})

    def verify(self) -> ChainReport:
        return verify_chain(self.records, anchor_hash=self.anchor_hash, anchor_seq=self.anchor_seq)


class ChainReport(_Frozen):
    ok: bool
    checked: int
    broken_at: int | None = None  # seq of the first bad record
    reason: Literal["seq_gap", "prev_hash", "hash", "time_order"] | None = None


def verify_chain(
    records: Sequence[AuditRecord],
    *,
    anchor_hash: str = GENESIS_HASH,
    anchor_seq: int = 0,
) -> ChainReport:
    """Check seq continuity, the prev_hash links, each record's own hash and time order."""
    prev_hash = anchor_hash
    expect = anchor_seq + 1
    prev_at: datetime | None = None
    for n, rec in enumerate(records):

        def bad(
            reason: Literal["seq_gap", "prev_hash", "hash", "time_order"],
            n: int = n,
            rec: AuditRecord = rec,
        ) -> ChainReport:
            return ChainReport(ok=False, checked=n, broken_at=rec.seq, reason=reason)

        if rec.seq != expect:
            return bad("seq_gap")
        if rec.prev_hash != prev_hash:
            return bad("prev_hash")
        if rec.hash != rec.compute_hash():
            return bad("hash")
        if prev_at is not None and rec.at < prev_at:
            return bad("time_order")
        prev_hash, expect, prev_at = rec.hash, expect + 1, rec.at
    return ChainReport(ok=True, checked=len(records))


def must_audit(profile: RetentionProfile, action: AuditAction, op: str = "") -> bool:
    """G-15: erase, consent and admin actions (forget, export, grant, retention expiry) are always
    audited; a correction is audited; other writes only where ``audit_writes``; reads only where
    ``audit_reads`` (multi_agent, regulated)."""
    if action in ("erase", "consent", "admin"):
        return True
    if action == "write":
        return op == "correct" or profile.audit_writes
    return profile.audit_reads


def record_audit(log: AuditLog, profile: RetentionProfile, entry: AuditEntry) -> AuditLog:
    """Append ``entry`` when the profile audits that action; otherwise return ``log`` unchanged."""
    return log.append(entry) if must_audit(profile, entry.action, entry.op) else log


def _entry(
    action: AuditAction,
    op: str,
    actor: str,
    ids: Iterable[str],
    at: datetime,
    purpose: str,
    outcome: AuditOutcome,
    reason: str | None,
    tenant: str | None,
) -> AuditEntry:
    return AuditEntry(
        actor=actor,
        action=action,
        op=op,
        ids=tuple(ids),
        purpose=purpose,
        at=at,
        outcome=outcome,
        reason=reason,
        tenant=tenant,
    )


def audit_read(
    actor: str,
    ids: Iterable[str],
    at: datetime,
    *,
    purpose: str = "*",
    op: str = "search",
    outcome: AuditOutcome = "ok",
    reason: str | None = None,
    tenant: str | None = None,
) -> AuditEntry:
    """A read: the record ids returned and the purpose (never the query text)."""
    return _entry("read", op, actor, ids, at, purpose, outcome, reason, tenant)


def audit_write(
    actor: str,
    ids: Iterable[str],
    at: datetime,
    *,
    purpose: str = "*",
    op: str = "remember",
    outcome: AuditOutcome = "ok",
    reason: str | None = None,
    tenant: str | None = None,
) -> AuditEntry:
    return _entry("write", op, actor, ids, at, purpose, outcome, reason, tenant)


def audit_erase(
    actor: str,
    ids: Iterable[str],
    at: datetime,
    *,
    purpose: str = "*",
    op: str = "forget",
    outcome: AuditOutcome = "ok",
    reason: str | None = None,
    tenant: str | None = None,
) -> AuditEntry:
    """An erasure: ids and a reason code only; the erased text is never recorded (ER-30)."""
    return _entry("erase", op, actor, ids, at, purpose, outcome, reason, tenant)


def audit_consent(
    actor: str,
    ids: Iterable[str],
    at: datetime,
    *,
    purpose: str = "*",
    op: str = "grant",
    outcome: AuditOutcome = "ok",
    reason: str | None = None,
    tenant: str | None = None,
) -> AuditEntry:
    return _entry("consent", op, actor, ids, at, purpose, outcome, reason, tenant)


def audit_admin(
    actor: str,
    ids: Iterable[str],
    at: datetime,
    *,
    purpose: str = "*",
    op: str = "export",
    outcome: AuditOutcome = "ok",
    reason: str | None = None,
    tenant: str | None = None,
) -> AuditEntry:
    return _entry("admin", op, actor, ids, at, purpose, outcome, reason, tenant)


def audit_retention_expiry(
    plan: RetentionPlan, *, actor: str = "retention", tenant: str | None = None
) -> AuditEntry | None:
    """The audit event for a retention pass: the expired ids (None when nothing expired)."""
    if not plan.expire:
        return None
    return _entry(
        "erase", "retention_expiry", actor, plan.expired_ids, plan.now, "*", "ok", None, tenant
    )


def prune_audit(log: AuditLog, now: datetime, profile: RetentionProfile) -> AuditLog:
    """Drop audit records older than the profile's audit retention, keeping the chain verifiable.

    Pruning only removes a prefix; the hash and seq of the last dropped record become the anchor.
    """
    ttl = parse_ttl(profile.audit_retention)
    if ttl is None or not log.records:
        return log
    cutoff = ttl.subtract_from(_aware(now, "now"))
    drop = 0
    for rec in log.records:
        if rec.at <= cutoff:
            drop += 1
        else:
            break
    if drop == 0:
        return log
    last = log.records[drop - 1]
    return AuditLog(records=log.records[drop:], anchor_hash=last.hash, anchor_seq=last.seq)
