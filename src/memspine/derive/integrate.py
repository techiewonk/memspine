"""Integration ladder and cardinality registry (LIB-02; W-21, W-22, M-12; S5-2 and its clarifications).

Pure and unwired (wiring is W2-10): no I/O, no clock, no model call. Depends on ``memspine.schemas`` only.

Design
------
``integrate(incoming, current, registry)`` answers "what does this assertion do to the stored ones?".
The answer is a pure function of the *set* of assertions about one ``(owner, subject, attribute)``, never of
their arrival order: the group is folded in event-time order (event start, else source time, else recorded
time, then source time, recorded time and occurrence id as tie-breaks), and the decision is the difference
between the fold over ``current + incoming`` and what is stored. An older-dated assertion that arrives
after a newer one therefore becomes history (its interval is closed at the newer value's start) instead of
superseding it (fixes INV-ORDER-SWAP).

Contract for the caller (W2-10)
-------------------------------
* ``current`` holds every stored occurrence of the group, including superseded, tentative, contested and
  *merged* occurrences. A merged occurrence is stored with ``status="hidden"`` and
  ``verdict.op="merge"``; the library keeps reading it so that a late-arriving assertion can re-resolve the
  group (otherwise a dropped duplicate would be lost information). Other hidden records are ignored.
* The record id of an envelope is ``record_id`` or, until it is assigned, ``occurrence_id``.
* Applying a decision means: store the incoming with ``incoming_status`` (and a verdict), apply
  ``closures`` (event interval end of the named records; ``end=None`` re-opens) and ``status_changes``. ``apply_decision`` does exactly that, purely.

Rules encoded (all from the 2026-10-13 sessions)
-----------------------------------------------
* Cardinality: declared by the registry; undeclared means ``many`` (Q7). Undeclared plus an extraction
  mark (``extras["transition"]`` truthy or ``extras["replaces"]``) contests instead of superseding (W-22).
* Tentative: anything not (asserted or negated) and bound: hypothetical, planned, reported, question,
  partly or unbound. Tentative content never changes another record.
* Cardinality one, asserted and bound: same value merges; ``source_trust`` at least the current value's
  supersedes (``can_supersede``); lower trust contests (both kept).
* Cardinality one, negated and bound, naming ``none`` or the current value: retires current values whose
  trust is at most its own (event interval closed, no new value); a higher-trust current value contests.
* A closed past interval (the assertion's own event end is set) coexists as history.
* Cardinality many: different values coexist; same value merges. A bound negation retires the same value.
* Same value, same source family: ``restated`` +1 (S4-3, never corroboration). Different family:
  ``independent`` +1.

Discrepancy with ``memspine.schemas.assessment``: ``is_tentative`` treats a bound *negated* assertion as
tentative, whereas the decided clarification retires/contests on it at cardinality one. This module
therefore keeps ``is_tentative`` for the other modalities but handles bound negation itself.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

from memspine.schemas.durable.assertion import (
    AssertionEnvelope,
    AssertionStatus,
    IntegrationVerdict,
)
from memspine.schemas.durable.assessment import can_supersede
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes

__all__ = [
    "DEFAULT_KIND_CAP",
    "OTHER_KIND",
    "Cardinality",
    "CardinalityRegistry",
    "Closure",
    "FinalState",
    "IntegrationDecision",
    "Outcome",
    "apply_decision",
    "integrate",
    "normalise_name",
    "ordering_key",
    "record_id",
    "replay",
]

Cardinality = Literal["one", "many"]
Outcome = Literal["append", "supersede", "retire", "merge", "coexist", "contest", "tentative"]

DEFAULT_KIND_CAP = 64
OTHER_KIND = "other"
_MIN = datetime(1, 1, 1, tzinfo=UTC)
_NONE_VALUES = frozenset({"", "none", "null", "nothing", "n/a"})


# --------------------------------------------------------------------------- names


def normalise_name(name: str) -> str:
    """Lower-case, snake_case, singular (S2-3 condition 3). Idempotent."""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name.strip())
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    if len(s) > 3:
        if s.endswith("ies"):
            s = s[:-3] + "y"
        elif s.endswith(("sses", "xes", "ches", "shes")):
            s = s[:-2]
        elif s.endswith("s") and not s.endswith(("ss", "us", "is")):
            s = s[:-1]
    return s


# --------------------------------------------------------------------------- registry


@dataclass(frozen=True)
class KindInfo:
    name: str
    cardinality: Cardinality
    provenance: Literal["declared", "model-proposed"]
    count: int


class CardinalityRegistry:
    """Attribute cardinality (M-12) plus the kind registry rules for model-proposed kinds (S2-3).

    * ``declare`` gives an attribute an operator-chosen cardinality (provenance ``declared``).
    * ``propose`` registers a model-proposed kind: normalised, cardinality ``many``, provenance
      ``model-proposed``; at most ``cap`` distinct proposed kinds per namespace, then ``other``.
    * ``cardinality`` for an unknown attribute is ``many`` (Q7: undeclared attributes coexist).
    """

    def __init__(self, cap: int = DEFAULT_KIND_CAP) -> None:
        if cap < 1:
            raise ValueError("cap must be at least 1")
        self.cap = cap
        self._declared: dict[str, dict[str, Cardinality]] = {}
        self._proposed: dict[str, dict[str, int]] = {}
        self._declared_counts: dict[str, dict[str, int]] = {}

    def declare(self, namespace: str, attribute: str, cardinality: Cardinality) -> str:
        if cardinality not in ("one", "many"):
            raise ValueError(f"cardinality must be 'one' or 'many', got {cardinality!r}")
        name = normalise_name(attribute)
        if not name:
            raise ValueError("attribute name is empty after normalisation")
        self._declared.setdefault(namespace, {})[name] = cardinality
        # A proposed kind that gets declared leaves the proposed pool (and frees a cap slot).
        self._proposed.get(namespace, {}).pop(name, None)
        return name

    def is_declared(self, namespace: str, attribute: str) -> bool:
        return normalise_name(attribute) in self._declared.get(namespace, {})

    def cardinality(self, namespace: str, attribute: str) -> Cardinality:
        return self._declared.get(namespace, {}).get(normalise_name(attribute), "many")

    def propose(self, namespace: str, kind: str) -> str:
        """Register (or count) a kind seen in model output and return the name to store."""
        name = normalise_name(kind) or OTHER_KIND
        declared = self._declared.get(namespace, {})
        if name in declared:
            self._declared_counts.setdefault(namespace, {}).setdefault(name, 0)
            self._declared_counts[namespace][name] += 1
            return name
        proposed = self._proposed.setdefault(namespace, {})
        if name not in proposed and len(proposed) >= self.cap:
            name = OTHER_KIND
        proposed[name] = proposed.get(name, 0) + 1
        return name

    def counts(self, namespace: str) -> dict[str, int]:
        """Occurrences per kind (declared and proposed) in the namespace."""
        out = dict(self._declared_counts.get(namespace, {}))
        out.update(self._proposed.get(namespace, {}))
        return out

    def entries(self, namespace: str) -> list[KindInfo]:
        rows: list[KindInfo] = []
        counts = self._declared_counts.get(namespace, {})
        for n, c in sorted(self._declared.get(namespace, {}).items()):
            rows.append(KindInfo(n, c, "declared", counts.get(n, 0)))
        for n, k in sorted(self._proposed.get(namespace, {}).items()):
            rows.append(KindInfo(n, "many", "model-proposed", k))
        return rows

    def proposed_count(self, namespace: str) -> int:
        return len(self._proposed.get(namespace, {}))


# --------------------------------------------------------------------------- decision types


@dataclass(frozen=True)
class Closure:
    """Set the event interval end of ``record_id`` (``end is None`` re-opens it)."""

    record_id: str
    end: datetime | None
    ref: TemporalRef | None = None  # ready-made inferred end (``None`` when re-opening)


@dataclass(frozen=True)
class IntegrationDecision:
    outcome: Outcome
    reason: str
    affected: tuple[str, ...] = ()
    closures: tuple[Closure, ...] = ()
    status_changes: tuple[tuple[str, AssertionStatus], ...] = ()
    incoming_status: AssertionStatus = "active"
    creates_value: bool = True
    merge_into: str | None = None
    restated_delta: int = 0
    independent_delta: int = 0
    cardinality: Cardinality = "many"
    declared: bool = False


# --------------------------------------------------------------------------- helpers on envelopes


def record_id(e: AssertionEnvelope) -> str:
    return e.record_id or e.occurrence_id


def _event_start(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    return ev.start.at if ev is not None and ev.start is not None else None


def _event_end(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    return ev.end.at if ev is not None and ev.end is not None else None


def _effective(e: AssertionEnvelope) -> datetime | None:
    """Event start, else source time, else recorded time, else event end."""
    return _event_start(e) or e.time.source_time or e.time.recorded_at or _event_end(e)


def ordering_key(e: AssertionEnvelope) -> tuple[datetime, datetime, datetime, str]:
    """Event time, then source time, then recorded time (then occurrence id for determinism)."""
    return (
        _effective(e) or _MIN,
        e.time.source_time or _MIN,
        e.time.recorded_at or _MIN,
        e.occurrence_id,
    )


def _subject_key(e: AssertionEnvelope) -> str | None:
    s = e.subject
    if s is None:
        return None
    return s.entity_id or s.surface.strip().casefold()


def _attr_key(e: AssertionEnvelope) -> str:
    return normalise_name(e.attribute or e.kind)


def _value_key(e: AssertionEnvelope) -> str:
    v = e.value if e.value is not None else e.text
    return str(v).strip().casefold()


def _family(e: AssertionEnvelope) -> str:
    for ev in e.evidence:
        if ev.role == "support":
            return ev.source_family
    for ev in e.evidence:
        if ev.counts_as_evidence:
            return ev.source_family
    return e.src_class


def _same_group(a: AssertionEnvelope, b: AssertionEnvelope) -> bool:
    return (
        a.scope.tenant == b.scope.tenant
        and a.scope.owner == b.scope.owner
        and _subject_key(a) == _subject_key(b)
        and _attr_key(a) == _attr_key(b)
    )


def _marked_transition(e: AssertionEnvelope) -> bool:
    return bool(e.extras.get("transition") or e.extras.get("replaces"))


def _tentative_reason(e: AssertionEnvelope) -> str | None:
    a = e.assessment
    if a.modality not in ("asserted", "negated"):
        return "modality"
    if a.binding != "bound":
        return "binding"
    return None


# --------------------------------------------------------------------------- the fold


@dataclass
class _Occ:
    env: AssertionEnvelope
    rid: str
    key: tuple[datetime, datetime, datetime, str]
    eff: datetime | None
    incoming: bool
    stored_status: AssertionStatus

    @property
    def trust(self) -> float:
        return self.env.assessment.source_trust


@dataclass
class _Group:
    occs: list[_Occ]
    start: datetime | None
    neg_contested: bool = False
    displaced_by: _Group | None = None
    retired_by: _Occ | None = None
    canonical: _Occ | None = None

    @property
    def value_key(self) -> str:
        return _value_key(self.occs[0].env)

    @property
    def top(self) -> _Occ:
        return max(self.occs, key=lambda o: o.trust)

    @property
    def trust(self) -> float:
        return self.top.trust


@dataclass
class _Res:
    kind: Literal[
        "live", "superseded", "merged", "past", "tentative", "retraction", "retraction_contested"
    ]
    status: AssertionStatus
    end: datetime | None = None
    start: datetime | None = None
    by: str | None = None  # displacer / merge target / retracted-by record id
    reason: str = ""
    peers: tuple[str, ...] = ()
    retired: tuple[str, ...] = ()
    canonical_of: str | None = None


def _mk_occ(e: AssertionEnvelope, incoming: bool) -> _Occ:
    return _Occ(
        env=e,
        rid=record_id(e),
        key=ordering_key(e),
        eff=_effective(e),
        incoming=incoming,
        stored_status=e.status,
    )


def _fold(occs: Sequence[_Occ], card: Cardinality, declared: bool) -> dict[str, _Res]:
    res: dict[str, _Res] = {}
    live: list[_Group] = []
    all_groups: list[_Group] = []
    negations: list[tuple[str, list[_Group], list[_Group]]] = []

    for o in sorted(occs, key=lambda x: x.key):
        env = o.env
        a = env.assessment
        why = _tentative_reason(env)
        if why is not None:
            res[o.rid] = _Res("tentative", "tentative", reason=why)
            continue
        # a node carrying its own closed interval is history (and a library-closed one is not "own")
        if _event_end(env) is not None and o.stored_status not in ("superseded", "hidden"):
            res[o.rid] = _Res("past", o.stored_status, reason="past_interval")
            continue
        neg = a.modality == "negated"
        vkey = _value_key(env)
        if neg:
            named_none = vkey in _NONE_VALUES
            targets = [g for g in live if (named_none and card == "one") or g.value_key == vkey]
            if not targets:
                res[o.rid] = _Res("tentative", "tentative", reason="negation_no_target")
                continue
            retired = [g for g in targets if g.trust <= o.trust]
            blocked = [g for g in targets if g.trust > o.trust]
            for g in retired:
                g.retired_by = o
                live.remove(g)
            for g in blocked:
                g.neg_contested = True
            res[o.rid] = _Res(
                "retraction" if retired else "retraction_contested",
                "active",
                reason="negation_retires" if retired else "negation_lower_trust",
            )
            negations.append((o.rid, retired, blocked))
            continue
        same = next((g for g in live if g.value_key == vkey), None)
        if same is not None:
            same.occs.append(o)
            continue
        g = _Group([o], o.eff)
        all_groups.append(g)
        if card == "one":
            mode = "one"
        elif not declared and _marked_transition(env):
            mode = "transition"
        else:
            mode = "many"
        if mode == "many":
            live.append(g)
            continue
        if mode == "transition":
            if live:
                for h in live:
                    h.neg_contested = True
                g.neg_contested = True
            live.append(g)
            continue
        lower = [h for h in live if can_supersede(env, h.top.env)]
        higher = [h for h in live if h not in lower]
        for h in lower:
            h.displaced_by = g
            live.remove(h)
        if higher:
            g.neg_contested = True
            for h in higher:
                h.neg_contested = True
        live.append(g)
    _finalise(res, all_groups)
    for rid, retired, blocked in negations:
        res[rid].retired = tuple(g.canonical.rid for g in retired if g.canonical is not None)
        res[rid].peers = tuple(g.canonical.rid for g in blocked if g.canonical is not None)
    return res


def _canonical(g: _Group) -> _Occ:
    """The earliest occurrence represents the value; later duplicates are merged into it."""
    return min(g.occs, key=lambda o: o.key)


def _finalise(res: dict[str, _Res], groups: list[_Group]) -> None:
    for g in groups:
        g.canonical = _canonical(g)
        if g.start is None:
            g.start = g.canonical.eff
    for g in groups:
        c = g.canonical
        assert c is not None
        for o in g.occs:
            if o is not c:
                res[o.rid] = _Res("merged", "hidden", by=c.rid, reason="merge")
        if g.displaced_by is not None or g.retired_by is not None:
            by = g.displaced_by.canonical if g.displaced_by is not None else g.retired_by
            assert by is not None
            by_start = (
                g.displaced_by.start
                if g.displaced_by is not None
                else (g.retired_by.eff if g.retired_by else None)
            )
            res[c.rid] = _Res("superseded", "superseded", end=by_start, start=g.start, by=by.rid)
        else:
            st: AssertionStatus = "contested" if g.neg_contested else "active"
            res[c.rid] = _Res("live", st, start=g.start)


# --------------------------------------------------------------------------- integrate


def _end_ref(end: datetime, by_env: AssertionEnvelope | None) -> TemporalRef:
    prec = "instant"
    if by_env is not None and by_env.time.event_precision != "unknown":
        prec = by_env.time.event_precision
    return TemporalRef(at=end, precision=prec, basis="inferred")  # type: ignore[arg-type]


def integrate(
    incoming: AssertionEnvelope,
    current: Iterable[AssertionEnvelope],
    registry: CardinalityRegistry,
) -> IntegrationDecision:
    """Decide what ``incoming`` does to ``current`` (see module docstring). Pure and order-independent."""
    ns = incoming.scope.tenant
    attr = _attr_key(incoming)
    declared = registry.is_declared(ns, attr)
    card = registry.cardinality(ns, attr)
    inc_rid = record_id(incoming)

    stored = [
        e
        for e in current
        if record_id(e) != inc_rid
        and e.occurrence_id != incoming.occurrence_id
        and _same_group(incoming, e)
        and (e.status != "hidden" or (e.verdict is not None and e.verdict.op == "merge"))
    ]
    mode_note = "undeclared_transition" if not declared and _marked_transition(incoming) else None

    occs = [_mk_occ(e, False) for e in stored] + [_mk_occ(incoming, True)]
    new = _fold(occs, card, declared)
    by_rid = {o.rid: o for o in occs}
    me = new[inc_rid]

    closures: list[Closure] = []
    changes: list[tuple[str, AssertionStatus]] = []
    for e in stored:
        rid = record_id(e)
        r = new[rid]
        if r.kind == "superseded":
            if r.end != _event_end(e):
                by_env = by_rid[r.by].env if r.by in by_rid else None
                closures.append(Closure(rid, r.end, _end_ref(r.end, by_env) if r.end else None))
        elif (
            r.kind == "live" and e.status in ("superseded", "hidden") and _event_end(e) is not None
        ):
            closures.append(Closure(rid, None))
        if r.status != e.status and r.kind != "past":
            changes.append((rid, r.status))
    if me.kind == "superseded" and me.end is not None:
        by_env = by_rid[me.by].env if me.by in by_rid else None
        closures.append(Closure(inc_rid, me.end, _end_ref(me.end, by_env)))

    base = dict(
        closures=tuple(closures),
        status_changes=tuple(changes),
        incoming_status=me.status,
        cardinality=card,
        declared=declared,
    )
    fam = _family(incoming)

    if me.kind == "tentative":
        live_ids = tuple(r_id for r_id, r in new.items() if r.kind == "live" and r_id != inc_rid)
        return IntegrationDecision(
            "tentative", me.reason, affected=live_ids, creates_value=False, **base
        )  # type: ignore[arg-type]
    if me.kind == "past":
        return IntegrationDecision("coexist", "past_interval", **base)  # type: ignore[arg-type]

    def _members(canon: str) -> list[_Occ]:
        return [
            o
            for o in occs
            if o.rid != inc_rid
            and (o.rid == canon or (new[o.rid].kind == "merged" and new[o.rid].by == canon))
        ]

    if me.kind == "merged":
        target = me.by or ""
        same_fam = any(_family(o.env) == fam for o in _members(target))
        return IntegrationDecision(
            "merge",
            "restated_same_family" if same_fam else "support_other_family",
            affected=(target,),
            creates_value=False,
            merge_into=target,
            restated_delta=1 if same_fam else 0,
            independent_delta=0 if same_fam else 1,
            **base,  # type: ignore[arg-type]
        )
    if me.kind == "retraction":
        return IntegrationDecision(
            "retire",
            "negation_retires",
            affected=me.retired,
            creates_value=False,
            **base,  # type: ignore[arg-type]
        )
    if me.kind == "retraction_contested":
        return IntegrationDecision(
            "contest",
            me.reason,
            affected=me.peers,
            creates_value=False,
            **base,  # type: ignore[arg-type]
        )
    if me.kind == "superseded":
        by_kind = new[me.by].kind if me.by in new else "live"
        outcome: Outcome = "retire" if by_kind == "retraction" else "supersede"
        return IntegrationDecision(outcome, "late_arrival_older", affected=(me.by or "",), **base)  # type: ignore[arg-type]

    # incoming is a live canonical value
    demoted = tuple(o.rid for o in occs if new[o.rid].kind == "merged" and new[o.rid].by == inc_rid)
    if demoted:  # earlier-dated duplicate of a stored value: it becomes the canonical occurrence
        same_fam = any(_family(o.env) == fam for o in _members(inc_rid))
        return IntegrationDecision(
            "merge",
            "earlier_occurrence",
            affected=demoted,
            restated_delta=1 if same_fam else 0,
            independent_delta=0 if same_fam else 1,
            **base,  # type: ignore[arg-type]
        )
    others = tuple(r_id for r_id, r in new.items() if r.kind == "live" and r_id != inc_rid)
    displaced = tuple(r_id for r_id, r in new.items() if r.kind == "superseded" and r.by == inc_rid)
    contested_with = others if me.status == "contested" else ()
    if contested_with:
        reason = mode_note or "lower_trust"
        return IntegrationDecision("contest", reason, affected=contested_with + displaced, **base)  # type: ignore[arg-type]
    if displaced:
        return IntegrationDecision("supersede", "newer_trust_ok", affected=displaced, **base)  # type: ignore[arg-type]
    if others:
        return IntegrationDecision(
            "coexist",
            "cardinality_many" if declared else "undeclared_cardinality",
            affected=others,
            **base,
        )  # type: ignore[arg-type]
    return IntegrationDecision("append", "first_value", **base)  # type: ignore[arg-type]


# --------------------------------------------------------------------------- applying and replay


def _with_end(e: AssertionEnvelope, closure: Closure) -> AssertionEnvelope:
    ev = e.time.event
    start = ev.start if ev is not None else None
    if closure.end is None:
        new_ev = Interval(start=start) if start is not None else None
    else:
        new_ev = Interval(start=start, end=closure.ref)
    return e.model_copy(update={"time": e.time.model_copy(update={"event": new_ev})})


def apply_decision(
    decision: IntegrationDecision, incoming: AssertionEnvelope, current: Sequence[AssertionEnvelope]
) -> list[AssertionEnvelope]:
    """Return ``current`` updated by ``decision`` plus the stored incoming (pure helper for W2-10 and tests)."""
    out: dict[str, AssertionEnvelope] = {record_id(e): e for e in current}
    for rid, status in decision.status_changes:
        if rid in out:
            cur = out[rid]
            upd: dict[str, object] = {"status": status}
            if status == "hidden":  # re-resolved as a duplicate of another value
                upd["verdict"] = IntegrationVerdict(op="merge", rung="reresolved")
            elif cur.status == "hidden":  # a merged occurrence became a value of its own
                upd["verdict"] = IntegrationVerdict(op="append", rung="reresolved")
            out[rid] = cur.model_copy(update=upd)
    inc = incoming
    for c in decision.closures:
        if c.record_id == record_id(inc):
            inc = _with_end(inc, c)
        elif c.record_id in out:
            out[c.record_id] = _with_end(out[c.record_id], c)
    op = {
        "append": "append",
        "supersede": "supersede",
        "retire": "supersede",
        "merge": "merge",
        "coexist": "coexist",
        "contest": "contest",
        "tentative": "noop",
    }[decision.outcome]
    inc = inc.model_copy(
        update={
            "status": decision.incoming_status,
            "verdict": IntegrationVerdict(op=op, rung=decision.reason),  # type: ignore[arg-type]
        }
    )
    out[record_id(inc)] = inc
    return list(out.values())


@dataclass(frozen=True)
class FinalState:
    """Order-independent summary of a stored set."""

    live: tuple[tuple[str, str], ...] = ()  # (attribute, value) of live asserted values
    contested: tuple[tuple[str, str], ...] = ()
    history: tuple[
        tuple[str, str, str, str], ...
    ] = ()  # (attribute, value, start, end) of closed values
    tentative: tuple[tuple[str, str], ...] = ()
    records: tuple[AssertionEnvelope, ...] = field(default=(), compare=False)


def summarise(records: Iterable[AssertionEnvelope]) -> FinalState:
    live: list[tuple[str, str]] = []
    contested: list[tuple[str, str]] = []
    hist: list[tuple[str, str, str, str]] = []
    tent: list[tuple[str, str]] = []
    recs = tuple(records)
    for e in recs:
        if e.status == "hidden":
            continue
        key = (f"{e.scope.tenant}/{e.scope.owner}/{_subject_key(e)}/{_attr_key(e)}", _value_key(e))
        if e.status == "tentative":
            tent.append(key)
            continue
        if e.assessment.modality != "asserted":
            continue
        end = _event_end(e)
        if end is None and e.status in ("active", "contested"):
            live.append(key)
            if e.status == "contested":
                contested.append(key)
        else:
            st = _effective(e)
            hist.append(
                (key[0], key[1], st.isoformat() if st else "", end.isoformat() if end else "")
            )
    return FinalState(
        tuple(sorted(live)),
        tuple(sorted(contested)),
        tuple(sorted(hist)),
        tuple(sorted(tent)),
        recs,
    )


def replay(
    assertions_in_any_order: Iterable[AssertionEnvelope], registry: CardinalityRegistry
) -> FinalState:
    """Integrate the assertions one by one in the given (arrival) order and summarise the stored set."""
    store: list[AssertionEnvelope] = []
    for a in assertions_in_any_order:
        store = apply_decision(integrate(a, store, registry), a, store)
    return summarise(store)


def status_map(records: Iterable[AssertionEnvelope]) -> Mapping[str, str]:
    return {record_id(e): e.status for e in records}


_ = TimeAxes  # re-exported type used by callers building envelopes
