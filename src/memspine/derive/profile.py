# ruff: noqa: E501
"""Profile card with guarded operations, preference drift and reflection plans (LIB-39; O-18, O-19, O-20, M-20, ER-28).

Pure and unwired (wiring is O4-07): no I/O, no clock, no model call. Opt-in layer (L4/L5): nothing here runs
unless the host turns the profile on. The card is the ``P^u`` of the notation: ``K_P`` (personalise) is the only
writer, and it writes only through :func:`apply_ops`.

Card (O-19, M-20 B)
-------------------
A ``ProfileCard`` has five sections (identity, preferences, relationships, routines, constraints / standing
instructions). Every entry carries its value, the support assertions it cites (with their trust), a stability
count (``restated``, S4-3: a stability signal, never corroboration), first and last confirmed dates, a
confidence, a status (``active`` / ``tentative`` / ``contested``) and a stance (``stated`` / ``inferred``).
The card is bounded by ``ProfileConfig.kappa_p`` (entry count, the L5 profile bound), ``max_chars`` and optional
per-section caps. Overflow is resolved by eviction on usefulness and stability (ER-28), never by refusing a
guarded op early; the evicted keys are reported.

Guarded operations (O-18 B)
---------------------------
A model proposes ``add`` / ``update`` / ``remove`` / ``keep`` at session close (O-04 call 2 / O-02). Code applies
an op only when it passes every guard, in this order of precedence:

* shape: known section, non-empty key and value within the size limit, aware ``observed_at`` (not after
  ``now`` if given), a finite ``signal`` in ``[0, 1]`` if present; a raw mapping carrying any system field
  (trust, support, restated, first_confirmed, status, confidence, ...) is rejected whole (``system_field``);
* evidence: at least one cited assertion, every id present in the evidence index, in the card's tenant and
  owner, and not ``hidden`` / ``superseded`` (``dead_evidence``); negated evidence never supports a value;
* stance: ``inferred`` ops are rejected unless ``allow_inferred``; an inferred op never overwrites a stated entry;
* S5-2: only *firm* evidence (asserted, bound, ``active``, not assistant-proposed) can create or change an
  ``active`` entry. Evidence that is tentative or contested (or hypothetical / planned / reported / question, or
  only assistant-proposed) can create a ``tentative`` entry and update another tentative entry, nothing else.
  Firm evidence promotes a tentative entry; the entry's support then lists the firm cites only;
* staleness: an op older than the entry's ``last_confirmed`` is rejected (``stale``);
* POL-2: the trust of an entry is the minimum over its supporting assertions. An op that contradicts a current
  value of higher trust does not overwrite it: it is recorded as a contest note on the entry
  (``status="contested"``, outcome ``contested``). A stated op replaces an inferred entry regardless;
* removal: needs a live retire or negation assertion about the entry's attribute (a ``negated`` assertion, or an
  asserted one with an event end). A removal of lower trust than the entry is a contest note, not a removal;
* ``keep`` with firm cites confirms (restated + 1 per new cite, ``last_confirmed`` advances); without cites it
  is a no-change acknowledgement.

Ops are applied in ``(observed_at, original index)`` order, so a batch gives the same card in any input order.

Drift (preference signal)
-------------------------
An op may carry a ``signal`` in ``[0, 1]`` (how strongly the dated evidence points one way). Accepted signals
update ``drift`` by ``P_{t+1} = alpha * p_t + (1 - alpha) * P_t`` in date order (:func:`ema_update`,
:func:`drift_ema`); the first signal initialises ``P``.

Reflection and invalidation
---------------------------
:func:`reflection_plan` lists the entries a reflection call should re-check (stale, contested, tentative,
low-support, dead support, flagged by an invalidation, or disagreeing with a value history from
``derive.histories``); it adds no facts. :func:`invalidate` (ER-28 B) removes changed assertion ids from the
support of every entry, keeps entries that still have independent support (flagged for re-check), and expires
entries left with none.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Literal

from memspine.derive.histories import ValueHistory
from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.steps.shared import ProfileOp as SchemaProfileOp

__all__ = [
    "SECTIONS",
    "ChangedGroup",
    "ContestNote",
    "DriftState",
    "InvalidationReport",
    "OpKind",
    "OpOutcome",
    "OpReport",
    "ProfileCard",
    "ProfileConfig",
    "ProfileEntry",
    "ProfileOp",
    "ReflectionItem",
    "ReflectionPlan",
    "SupportRef",
    "apply_ops",
    "drift_ema",
    "ema_update",
    "empty_card",
    "from_schema_op",
    "invalidate",
    "parse_op",
    "record_use",
    "reflection_plan",
]

Section = Literal["identity", "preferences", "relationships", "routines", "constraints"]
SECTIONS: tuple[str, ...] = ("identity", "preferences", "relationships", "routines", "constraints")
_SECTION_RANK = {s: i for i, s in enumerate(SECTIONS)}
OpKind = Literal["add", "update", "remove", "keep"]
Status = Literal["active", "tentative", "contested"]
Stance = Literal["stated", "inferred"]

_OP_FIELDS = frozenset(
    {"op", "kind", "section", "key", "value", "observed_at", "cites", "stance", "signal"}
)
_MAX_CONTESTS = 3


# --------------------------------------------------------------------------- data


@dataclass(frozen=True)
class ProfileConfig:
    """Bounds and switches. ``kappa_p`` is the L5 profile bound (maximum number of entries)."""

    kappa_p: int = 24
    max_chars: int = 2400
    max_value_chars: int = 240
    section_caps: tuple[tuple[str, int], ...] = ()
    alpha: float = 0.3  # EMA gain for preference drift
    allow_inferred: bool = False  # O-19: stated-only by default
    stale_after_days: float = 180.0
    min_support: int = 1
    low_confidence: float = 0.5

    def __post_init__(self) -> None:
        if self.kappa_p < 1 or self.max_chars < 1 or self.max_value_chars < 1:
            raise ValueError("kappa_p, max_chars and max_value_chars must be positive")
        if not 0.0 < self.alpha <= 1.0:
            raise ValueError("alpha must be in (0, 1]")
        for sec, cap in self.section_caps:
            if sec not in _SECTION_RANK or cap < 0:
                raise ValueError(f"bad section cap {sec!r}={cap}")

    def cap_for(self, section: str) -> int | None:
        for sec, cap in self.section_caps:
            if sec == section:
                return cap
        return None


@dataclass(frozen=True)
class SupportRef:
    """One supporting assertion: its id and the trust it had when cited (POL-2 input)."""

    assertion_id: str
    trust: float


@dataclass(frozen=True)
class ContestNote:
    """A rival value (``None`` = a rival removal) that did not overwrite the current value."""

    value: str | None
    assertion_ids: tuple[str, ...]
    trust: float
    at: datetime


@dataclass(frozen=True)
class DriftState:
    value: float
    n: int
    last_at: datetime


@dataclass(frozen=True)
class ProfileEntry:
    section: str
    key: str
    value: str
    support: tuple[SupportRef, ...]
    restated: int
    first_confirmed: datetime
    last_confirmed: datetime
    status: Status = "active"
    stance: Stance = "stated"
    attrs: tuple[str, ...] = ()  # attributes of the cited assertions, used to match value histories
    contests: tuple[ContestNote, ...] = ()
    drift: DriftState | None = None
    uses: int = 0
    needs_recheck: bool = False

    @property
    def support_ids(self) -> tuple[str, ...]:
        return tuple(s.assertion_id for s in self.support)

    @property
    def trust(self) -> float:
        """POL-2: the minimum over the supporting assertions (1.0 only if there are none, which never persists)."""
        return min((s.trust for s in self.support), default=0.0)

    @property
    def confidence(self) -> float:
        base = self.trust * (0.75 + 0.25 * (1.0 - 0.5**self.restated))
        if self.status == "tentative":
            base *= 0.5
        elif self.status == "contested":
            base *= 0.7
        return round(min(1.0, max(0.0, base)), 9)

    @property
    def size(self) -> int:
        return len(self.key) + len(self.value)


@dataclass(frozen=True)
class ProfileCard:
    tenant: str
    owner: str
    entries: tuple[ProfileEntry, ...] = ()

    def get(self, section: str, key: str) -> ProfileEntry | None:
        for e in self.entries:
            if e.section == section and e.key == key:
                return e
        return None

    def section(self, name: str) -> tuple[ProfileEntry, ...]:
        return tuple(e for e in self.entries if e.section == name)

    @property
    def chars(self) -> int:
        return sum(e.size for e in self.entries)

    def within(self, config: ProfileConfig) -> bool:
        if len(self.entries) > config.kappa_p or self.chars > config.max_chars:
            return False
        return all(
            cap is None or len(self.section(s)) <= cap
            for s in SECTIONS
            for cap in (config.cap_for(s),)
        )


def empty_card(tenant: str, owner: str) -> ProfileCard:
    return ProfileCard(tenant=tenant, owner=owner)


@dataclass(frozen=True)
class ProfileOp:
    """A proposed operation. Carries content fields only; there is nowhere to put a system field."""

    kind: OpKind
    section: str
    key: str
    observed_at: datetime
    value: str | None = None
    cites: tuple[str, ...] = ()
    stance: Stance = "stated"
    signal: float | None = None


@dataclass(frozen=True)
class OpOutcome:
    index: int
    kind: str
    section: str
    key: str
    status: Literal["applied", "contested", "noop", "rejected"]
    reason: str


@dataclass(frozen=True)
class OpReport:
    outcomes: tuple[OpOutcome, ...] = ()
    evicted: tuple[tuple[str, str], ...] = ()

    def count(self, status: str) -> int:
        return sum(1 for o in self.outcomes if o.status == status)

    @property
    def applied(self) -> int:
        return self.count("applied")

    @property
    def rejected(self) -> int:
        return self.count("rejected")

    @property
    def contested(self) -> int:
        return self.count("contested")

    def reasons(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            if o.status == "rejected":
                out[o.reason] = out.get(o.reason, 0) + 1
        return dict(sorted(out.items()))


# --------------------------------------------------------------------------- op parsing


def parse_op(raw: Mapping[str, object]) -> ProfileOp | str:
    """Parse a model-proposed mapping. Returns the op, or a rejection reason (never raises)."""
    unknown = sorted(str(k) for k in raw if k not in _OP_FIELDS)
    if unknown:
        return f"system_field:{','.join(unknown)}"
    kind = raw.get("op", raw.get("kind"))
    if kind not in ("add", "update", "remove", "keep"):
        return "bad_kind"
    section = raw.get("section")
    key = raw.get("key")
    if not isinstance(section, str) or not isinstance(key, str):
        return "bad_op"
    value = raw.get("value")
    if value is not None and not isinstance(value, str):
        return "bad_op"
    obs = raw.get("observed_at")
    if isinstance(obs, str):
        try:
            obs = datetime.fromisoformat(obs)
        except ValueError:
            return "bad_time"
    if not isinstance(obs, datetime):
        return "bad_time"
    cites_raw = raw.get("cites", ())
    if isinstance(cites_raw, str) or not isinstance(cites_raw, Iterable):
        return "bad_op"
    cites = tuple(c for c in cites_raw)
    if not all(isinstance(c, str) for c in cites):
        return "bad_op"
    stance = raw.get("stance", "stated")
    if stance not in ("stated", "inferred"):
        return "bad_op"
    signal = raw.get("signal")
    if signal is not None and (isinstance(signal, bool) or not isinstance(signal, (int, float))):
        return "bad_signal"
    return ProfileOp(
        kind=kind,  # type: ignore[arg-type]
        section=section,
        key=key,
        observed_at=obs,
        value=value,
        cites=cites,  # type: ignore[arg-type]
        stance=stance,  # type: ignore[arg-type]
        signal=float(signal) if signal is not None else None,
    )


_SCHEMA_SECTION = {
    "identity": "identity",
    "attribute": "preferences",
    "relationship": "relationships",
    "instruction": "constraints",
}


def from_schema_op(op: SchemaProfileOp) -> ProfileOp | str:
    """Map the wire ``ProfileOp`` (delete / none, four entry types) onto this module's op; ``none`` is ``keep``."""
    kind = {"add": "add", "update": "update", "delete": "remove", "none": "keep"}[op.op]
    return parse_op(
        {
            "op": kind,
            "section": _SCHEMA_SECTION[op.entry_type],
            "key": op.key,
            "value": op.value if kind in ("add", "update") else None,
            "observed_at": op.observed_at,
            "cites": tuple(op.cites),
            "stance": op.stance,
        }
    )


# --------------------------------------------------------------------------- drift


def ema_update(prev: float | None, p: float, alpha: float) -> float:
    """``P_{t+1} = alpha * p_t + (1 - alpha) * P_t``; the first observation initialises ``P``."""
    if not 0.0 < alpha <= 1.0:
        raise ValueError("alpha must be in (0, 1]")
    if prev is None:
        return p
    return alpha * p + (1.0 - alpha) * prev


def drift_ema(
    observations: Iterable[tuple[datetime, float]], alpha: float, prior: float | None = None
) -> float | None:
    """EMA over dated observations, in date order (ties keep input order). ``None`` if there are none."""
    ordered = sorted(enumerate(observations), key=lambda t: (t[1][0], t[0]))
    state = prior
    for _, (_, p) in ordered:
        state = ema_update(state, p, alpha)
    return state


# --------------------------------------------------------------------------- helpers


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def _order(e: ProfileEntry) -> tuple[int, str]:
    return (_SECTION_RANK.get(e.section, 99), e.key)


def _sorted(entries: Iterable[ProfileEntry]) -> tuple[ProfileEntry, ...]:
    return tuple(sorted(entries, key=_order))


def _rid(e: AssertionEnvelope) -> str:
    return e.record_id or e.occurrence_id


def _is_retire(e: AssertionEnvelope) -> bool:
    if e.status != "active" or e.assessment.binding != "bound":
        return False
    if e.assessment.modality == "negated":
        return True
    ev = e.time.event
    return e.assessment.modality == "asserted" and ev is not None and ev.end is not None


def _is_firm(e: AssertionEnvelope) -> bool:
    a = e.assessment
    return (
        e.status == "active"
        and a.modality == "asserted"
        and a.binding == "bound"
        and e.src_class != "assistant_proposed"
    )


def _drift(prev: DriftState | None, op: ProfileOp, alpha: float) -> DriftState | None:
    if op.signal is None:
        return prev
    return DriftState(
        value=ema_update(prev.value if prev else None, op.signal, alpha),
        n=(prev.n if prev else 0) + 1,
        last_at=op.observed_at,
    )


def _cites(op: ProfileOp) -> tuple[str, ...]:
    return tuple(dict.fromkeys(op.cites))


# --------------------------------------------------------------------------- apply


class _Reject(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def apply_ops(
    card: ProfileCard,
    proposed_ops: Iterable[ProfileOp | Mapping[str, object] | SchemaProfileOp],
    evidence_index: Mapping[str, AssertionEnvelope],
    *,
    config: ProfileConfig | None = None,
    now: datetime | None = None,
    usefulness: Mapping[tuple[str, str], float] | None = None,
) -> tuple[ProfileCard, OpReport]:
    """Apply guarded ops to ``card`` and return ``(card', report)``. ``card`` is never mutated."""
    cfg = config or ProfileConfig()
    parsed: list[tuple[int, ProfileOp | None, str, str, str]] = []
    outcomes: list[OpOutcome] = []
    for i, raw in enumerate(proposed_ops):
        if isinstance(raw, SchemaProfileOp):
            raw = from_schema_op(raw)
        if isinstance(raw, Mapping):
            sec, key = str(raw.get("section", "")), str(raw.get("key", ""))
            kind = str(raw.get("op", raw.get("kind", "")))
            raw = parse_op(raw)
        elif isinstance(raw, ProfileOp):
            sec, key, kind = raw.section, raw.key, raw.kind
        else:  # a rejection string coming from from_schema_op
            sec, key, kind = "", "", ""
        if isinstance(raw, str):
            outcomes.append(OpOutcome(i, kind, sec, key, "rejected", raw))
            continue
        parsed.append((i, raw, kind, sec, key))
    # naive datetimes are rejected by _shape, so they sort first on a safe key
    parsed.sort(key=lambda t: (_ts(t[1]), t[0]))

    entries: dict[tuple[str, str], ProfileEntry] = {(e.section, e.key): e for e in card.entries}
    for i, op, _, _, _ in parsed:
        assert op is not None
        try:
            status, reason = _apply_one(card, entries, op, evidence_index, cfg, now)
        except _Reject as r:
            outcomes.append(OpOutcome(i, op.kind, op.section, op.key, "rejected", r.reason))
            continue
        outcomes.append(OpOutcome(i, op.kind, op.section, op.key, status, reason))

    kept, evicted = _enforce_bounds(entries, cfg, usefulness or {})
    outcomes.sort(key=lambda o: o.index)
    return (
        ProfileCard(card.tenant, card.owner, _sorted(kept.values())),
        OpReport(tuple(outcomes), tuple(evicted)),
    )


def _ts(op: ProfileOp | None) -> float:
    if op is None or op.observed_at.tzinfo is None:
        return -math.inf
    return op.observed_at.timestamp()


def _shape(op: ProfileOp, cfg: ProfileConfig, now: datetime | None) -> None:
    if op.section not in _SECTION_RANK:
        raise _Reject("bad_section")
    if not op.key.strip() or len(op.key) > 80:
        raise _Reject("bad_key")
    if op.kind in ("add", "update"):
        if op.value is None or not op.value.strip():
            raise _Reject("empty_value")
        if len(op.value) > cfg.max_value_chars:
            raise _Reject("value_too_long")
    elif op.value is not None and op.value.strip():
        raise _Reject("unexpected_value")
    if op.observed_at.tzinfo is None:
        raise _Reject("bad_time")
    if now is not None and op.observed_at > now:
        raise _Reject("future")
    if op.signal is not None and (not math.isfinite(op.signal) or not 0.0 <= op.signal <= 1.0):
        raise _Reject("bad_signal")
    if op.stance == "inferred" and not cfg.allow_inferred:
        raise _Reject("inferred_disabled")


def _evidence(
    card: ProfileCard, op: ProfileOp, index: Mapping[str, AssertionEnvelope], *, need_cites: bool
) -> list[AssertionEnvelope]:
    ids = _cites(op)
    if not ids:
        if need_cites:
            raise _Reject("uncited")
        return []
    found: list[AssertionEnvelope] = []
    for cid in ids:
        e = index.get(cid)
        if e is None:
            raise _Reject("unknown_evidence")
        if e.scope.tenant != card.tenant or e.scope.owner != card.owner:
            raise _Reject("foreign_scope")
        if e.status in ("hidden", "superseded"):
            raise _Reject("dead_evidence")
        found.append(e)
    return found


def _refs(es: Iterable[AssertionEnvelope]) -> tuple[SupportRef, ...]:
    return tuple(SupportRef(_rid(e), e.assessment.source_trust) for e in es)


def _stability(es: Iterable[AssertionEnvelope]) -> int:
    es = list(es)
    return max(0, len(es) - 1) + sum(e.assessment.support.restated for e in es)


def _attrs(*groups: Iterable[str], extra: Iterable[AssertionEnvelope] = ()) -> tuple[str, ...]:
    out: set[str] = set()
    for g in groups:
        out.update(g)
    out.update(e.attribute for e in extra if e.attribute)
    return tuple(sorted(out))


def _apply_one(
    card: ProfileCard,
    entries: dict[tuple[str, str], ProfileEntry],
    op: ProfileOp,
    index: Mapping[str, AssertionEnvelope],
    cfg: ProfileConfig,
    now: datetime | None,
) -> tuple[Literal["applied", "contested", "noop"], str]:
    _shape(op, cfg, now)
    slot = (op.section, op.key)
    cur = entries.get(slot)

    if op.kind == "keep":
        return _keep(card, entries, op, cur, index, cfg)
    if op.kind == "remove":
        return _remove(card, entries, op, cur, index)
    if op.kind == "update" and cur is None:
        raise _Reject("no_such_entry")

    cited = _evidence(card, op, index, need_cites=True)
    if any(e.assessment.modality == "negated" for e in cited):
        raise _Reject("negated_evidence")
    firm = [e for e in cited if _is_firm(e)]
    assert op.value is not None
    value = op.value.strip()

    if cur is not None and op.observed_at < cur.last_confirmed:
        raise _Reject("stale")
    if cur is not None and op.stance == "inferred" and cur.stance == "stated":
        raise _Reject("inferred_over_stated")

    if not firm:  # S5-2: tentative evidence can only create or move tentative entries
        soft = cited
        if cur is None:
            entries[slot] = ProfileEntry(
                op.section, op.key, value, _refs(soft), _stability(soft), op.observed_at,
                op.observed_at, status="tentative", stance=op.stance,
                attrs=_attrs(extra=soft), drift=_drift(None, op, cfg.alpha),
            )  # fmt: skip
            return "applied", "created_tentative"
        if cur.status != "tentative":
            if _norm(cur.value) == _norm(value):
                return "noop", "tentative_evidence_no_confirmation"
            raise _Reject("tentative_cannot_update")
        entries[slot] = replace(
            cur, value=value, support=_merge_refs(cur.support, _refs(soft)),
            restated=cur.restated + _new_count(cur, soft), last_confirmed=op.observed_at,
            attrs=_attrs(cur.attrs, extra=soft), drift=_drift(cur.drift, op, cfg.alpha),
            needs_recheck=False,
        )  # fmt: skip
        return "applied", "updated_tentative"

    trust = min(e.assessment.source_trust for e in firm)
    if cur is None:
        entries[slot] = ProfileEntry(
            op.section, op.key, value, _refs(firm), _stability(firm), op.observed_at,
            op.observed_at, stance=op.stance, attrs=_attrs(extra=firm),
            drift=_drift(None, op, cfg.alpha),
        )  # fmt: skip
        return "applied", "created"

    same = _norm(cur.value) == _norm(value)
    if same:
        entries[slot] = _confirm(cur, firm, op, cfg, promote=True)
        return "applied", "confirmed"
    if cur.status == "tentative":  # firm evidence promotes a tentative entry
        entries[slot] = ProfileEntry(
            op.section, op.key, value, _refs(firm), _stability(firm), cur.first_confirmed,
            op.observed_at, stance=op.stance, attrs=_attrs(extra=firm),
            drift=_drift(cur.drift, op, cfg.alpha), uses=cur.uses,
        )  # fmt: skip
        return "applied", "promoted"
    rival_floor = (
        max([cur.trust, *(c.trust for c in cur.contests)])
        if cur.status == "contested"
        else cur.trust
    )
    stated_over_inferred = cur.stance == "inferred" and op.stance == "stated"
    if trust >= rival_floor or stated_over_inferred:
        entries[slot] = ProfileEntry(
            op.section, op.key, value, _refs(firm), _stability(firm), cur.first_confirmed,
            op.observed_at, stance=op.stance, attrs=_attrs(extra=firm),
            drift=_drift(cur.drift, op, cfg.alpha), uses=cur.uses,
        )  # fmt: skip
        return "applied", "replaced"
    note = ContestNote(value, tuple(_rid(e) for e in firm), trust, op.observed_at)
    entries[slot] = _with_contest(cur, note)
    return "contested", "lower_trust_conflict"


def _merge_refs(a: tuple[SupportRef, ...], b: tuple[SupportRef, ...]) -> tuple[SupportRef, ...]:
    seen = {s.assertion_id for s in a}
    return a + tuple(s for s in b if s.assertion_id not in seen)


def _new_count(cur: ProfileEntry, es: Iterable[AssertionEnvelope]) -> int:
    have = set(cur.support_ids)
    fresh = [e for e in es if _rid(e) not in have]
    return len(fresh) + sum(e.assessment.support.restated for e in fresh)


def _with_contest(cur: ProfileEntry, note: ContestNote) -> ProfileEntry:
    kept = tuple(c for c in cur.contests if c.value != note.value)
    contests = (*kept, note)[-_MAX_CONTESTS:]
    return replace(
        cur, contests=contests, status="tentative" if cur.status == "tentative" else "contested"
    )


def _confirm(
    cur: ProfileEntry,
    firm: Sequence[AssertionEnvelope],
    op: ProfileOp,
    cfg: ProfileConfig,
    *,
    promote: bool,
) -> ProfileEntry:
    status: Status = "active" if promote and cur.status == "tentative" else cur.status
    support = (
        _refs(firm)
        if cur.status == "tentative" and promote
        else _merge_refs(cur.support, _refs(firm))
    )
    restated = 0 if cur.status == "tentative" and promote else cur.restated
    restated += _stability(firm) if cur.status == "tentative" and promote else _new_count(cur, firm)
    return replace(
        cur,
        status=status,
        support=support,
        restated=restated,
        last_confirmed=max(cur.last_confirmed, op.observed_at),
        attrs=_attrs(cur.attrs, extra=firm),
        drift=_drift(cur.drift, op, cfg.alpha),
        needs_recheck=False,
        stance="stated" if op.stance == "stated" else cur.stance,
    )


def _keep(
    card: ProfileCard,
    entries: dict[tuple[str, str], ProfileEntry],
    op: ProfileOp,
    cur: ProfileEntry | None,
    index: Mapping[str, AssertionEnvelope],
    cfg: ProfileConfig,
) -> tuple[Literal["applied", "contested", "noop"], str]:
    if cur is None:
        raise _Reject("no_such_entry")
    cited = _evidence(card, op, index, need_cites=False)
    if any(e.assessment.modality == "negated" for e in cited):
        raise _Reject("negated_evidence")
    firm = [e for e in cited if _is_firm(e)]
    if not firm or op.observed_at < cur.last_confirmed:
        return "noop", "kept"
    # a keep restates the current value; it never changes it, and tentative entries stay tentative
    entries[(op.section, op.key)] = _confirm(cur, firm, op, cfg, promote=False)
    return "applied", "confirmed"


def _remove(
    card: ProfileCard,
    entries: dict[tuple[str, str], ProfileEntry],
    op: ProfileOp,
    cur: ProfileEntry | None,
    index: Mapping[str, AssertionEnvelope],
) -> tuple[Literal["applied", "contested", "noop"], str]:
    if cur is None:
        raise _Reject("no_such_entry")
    cited = _evidence(card, op, index, need_cites=True)
    retire = [e for e in cited if _is_retire(e)]
    if not retire:
        raise _Reject("no_retire_evidence")
    if cur.attrs and any(e.attribute and e.attribute not in cur.attrs for e in retire):
        raise _Reject("mismatched_evidence")
    if op.observed_at < cur.last_confirmed:
        raise _Reject("stale")
    trust = min(e.assessment.source_trust for e in retire)
    if cur.status != "tentative" and trust < cur.trust:
        note = ContestNote(None, tuple(_rid(e) for e in retire), trust, op.observed_at)
        entries[(op.section, op.key)] = _with_contest(cur, note)
        return "contested", "lower_trust_removal"
    del entries[(op.section, op.key)]
    return "applied", "removed"


# --------------------------------------------------------------------------- bounds


def _keep_score(
    e: ProfileEntry, uses: Mapping[tuple[str, str], float]
) -> tuple[float, float, int, str]:
    useful = float(e.uses) + float(uses.get((e.section, e.key), 0.0))
    score = 2.0 * math.log1p(max(useful, 0.0)) + math.log1p(e.restated) + e.confidence
    if e.section == "constraints":
        score += 1.0  # standing instructions are protected
    if e.status == "tentative":
        score -= 0.8
    return (round(score, 9), e.last_confirmed.timestamp(), _SECTION_RANK.get(e.section, 99), e.key)  # type: ignore[return-value]


def _enforce_bounds(
    entries: dict[tuple[str, str], ProfileEntry],
    cfg: ProfileConfig,
    uses: Mapping[tuple[str, str], float],
) -> tuple[dict[tuple[str, str], ProfileEntry], list[tuple[str, str]]]:
    evicted: list[tuple[str, str]] = []

    def drop_lowest(pool: list[tuple[str, str]]) -> None:
        victim = min(pool, key=lambda s: _keep_score(entries[s], uses))
        del entries[victim]
        evicted.append(victim)

    for sec in SECTIONS:
        cap = cfg.cap_for(sec)
        while cap is not None:
            pool = [s for s in entries if s[0] == sec]
            if len(pool) <= cap:
                break
            drop_lowest(pool)
    while entries and (
        len(entries) > cfg.kappa_p or sum(e.size for e in entries.values()) > cfg.max_chars
    ):
        drop_lowest(list(entries))
    return entries, evicted


def record_use(card: ProfileCard, used: Iterable[tuple[str, str]]) -> ProfileCard:
    """Count that these entries were injected and helped (ER-28 usefulness). Pure; returns a new card."""
    counts: dict[tuple[str, str], int] = {}
    for slot in used:
        counts[slot] = counts.get(slot, 0) + 1
    return ProfileCard(
        card.tenant,
        card.owner,
        tuple(replace(e, uses=e.uses + counts.get((e.section, e.key), 0)) for e in card.entries),
    )


# --------------------------------------------------------------------------- reflection plan


@dataclass(frozen=True)
class ReflectionItem:
    section: str
    key: str
    reasons: tuple[str, ...]
    priority: float
    support_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReflectionPlan:
    items: tuple[ReflectionItem, ...] = ()

    def keys(self) -> tuple[tuple[str, str], ...]:
        return tuple((i.section, i.key) for i in self.items)


_REASON_WEIGHT = {
    "contested": 3.0,
    "history_conflict": 3.0,
    "contested_in_history": 2.5,
    "changed_group": 2.5,
    "dead_support": 2.5,
    "history_closed": 2.0,
    "stale": 1.5,
    "tentative": 1.0,
    "low_support": 1.0,
    "low_confidence": 0.5,
}


def reflection_plan(
    card: ProfileCard,
    histories: ValueHistory | None,
    *,
    now: datetime,
    config: ProfileConfig | None = None,
    evidence_index: Mapping[str, AssertionEnvelope] | None = None,
) -> ReflectionPlan:
    """List the entries a reflection call should re-check, most urgent first. Adds no facts."""
    cfg = config or ProfileConfig()
    items: list[ReflectionItem] = []
    for e in card.entries:
        reasons: list[str] = []
        if (now - e.last_confirmed).total_seconds() > cfg.stale_after_days * 86400:
            reasons.append("stale")
        if e.status == "contested" or e.contests:
            reasons.append("contested")
        if e.status == "tentative":
            reasons.append("tentative")
        if len(e.support) < cfg.min_support:
            reasons.append("low_support")
        if e.confidence < cfg.low_confidence and e.status == "active":
            reasons.append("low_confidence")
        if e.needs_recheck:
            reasons.append("changed_group")
        if evidence_index is not None and any(
            (ev := evidence_index.get(s)) is None or ev.status in ("hidden", "superseded")
            for s in e.support_ids
        ):
            reasons.append("dead_support")
        if histories is not None and e.attrs:
            reasons.extend(_history_reasons(card, e, histories))
        if reasons:
            uniq = tuple(sorted(set(reasons), key=lambda r: (-_REASON_WEIGHT.get(r, 0.0), r)))
            prio = round(sum(_REASON_WEIGHT.get(r, 0.0) for r in uniq), 9)
            items.append(ReflectionItem(e.section, e.key, uniq, prio, e.support_ids))
    items.sort(key=lambda i: (-i.priority, _SECTION_RANK.get(i.section, 99), i.key))
    return ReflectionPlan(tuple(items))


def _history_reasons(card: ProfileCard, e: ProfileEntry, histories: ValueHistory) -> list[str]:
    out: list[str] = []
    for line in histories.lines:
        k = line.key
        if k.tenant != card.tenant or k.owner != card.owner or k.attribute not in e.attrs:
            continue
        if line.open_contests():
            out.append("contested_in_history")
        cur = line.current()
        if not cur:
            if line.spans and not line.open_contests():
                out.append("history_closed")
            continue
        if all(_norm(s.value) != _norm(e.value) for s in cur):
            out.append("history_conflict")
    return out


# --------------------------------------------------------------------------- invalidation


@dataclass(frozen=True)
class ChangedGroup:
    """A source group that changed (superseded, hidden, retracted or erased).

    ``assertion_ids`` are removed from the support of every entry that cites them. ``attributes`` only flag
    entries on those attributes for re-check, since no specific citation is known to be gone.
    """

    assertion_ids: frozenset[str] = frozenset()
    attributes: frozenset[str] = frozenset()


@dataclass(frozen=True)
class InvalidationReport:
    expired: tuple[tuple[str, str], ...] = ()
    reduced: tuple[tuple[str, str], ...] = ()
    flagged: tuple[tuple[str, str], ...] = ()


def invalidate(
    card: ProfileCard, changed_groups: Iterable[ChangedGroup]
) -> tuple[ProfileCard, InvalidationReport]:
    """ER-28 B: drop changed ids from support, keep independent support, expire entries left with none."""
    groups = list(changed_groups)
    gone: set[str] = set()
    attrs: set[str] = set()
    for g in groups:
        gone |= set(g.assertion_ids)
        attrs |= set(g.attributes)
    kept: list[ProfileEntry] = []
    expired: list[tuple[str, str]] = []
    reduced: list[tuple[str, str]] = []
    flagged: list[tuple[str, str]] = []
    for e in card.entries:
        slot = (e.section, e.key)
        remaining = tuple(s for s in e.support if s.assertion_id not in gone)
        removed = len(e.support) - len(remaining)
        if removed and not remaining:
            expired.append(slot)
            continue
        live_contests = tuple(c for c in e.contests if not set(c.assertion_ids) & gone)
        new = replace(e, contests=live_contests)
        if removed:
            new = replace(
                new,
                support=remaining,
                restated=max(0, e.restated - removed),
                needs_recheck=True,
            )
            reduced.append(slot)
        elif attrs & set(e.attrs):
            new = replace(new, needs_recheck=True)
            flagged.append(slot)
        if new.status == "contested" and not new.contests:
            new = replace(new, status="active")
        kept.append(new)
    return (
        ProfileCard(card.tenant, card.owner, _sorted(kept)),
        InvalidationReport(tuple(expired), tuple(reduced), tuple(flagged)),
    )
