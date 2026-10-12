# ruff: noqa: E501
"""Time view and two-axis as-of reads (LIB-22; decisions R-24, M-11, S6-3).

Pure and unwired (wired later by R3-14). Standard library, pydantic and ``memspine.schemas`` only (no other memspine layer; no I/O, no clock read ("now" is never needed), no model call.

Two axes (S6-3)
---------------
* *event time* answers "what was true on X" (``as_of_valid``);
* *source and recorded time* answer "what did we know on X" (``as_of_known``, caller-set only, M-11 B).

Modes of :class:`TimeView`
--------------------------
``current``   live values: status active or contested, asserted, event interval not closed.
``true_at``   event time overlaps the point or period asked about.
``known_at``  every available source/recorded stamp is ``<= t``. The state is re-derived from the known
              subset only (see below), so a correction learned later cannot leak in.
``both``      known at ``t2`` first, then true at ``t1`` judged on the state as known at ``t2``.
``history``   every non-hidden asserted value, ordered by event time (arrival order is irrelevant).
``range``     event time overlaps ``[a, b)``.

Three-valued time
-----------------
Every comparison is three-valued (LIB-01). ``False`` removes an assertion (listed in ``out_of_window``),
``True`` keeps it, ``None`` (no event time, an unresolved relation, an approximate date that straddles the
boundary, a missing stamp) keeps it with ``uncertain_time=True``. Nothing is silently dropped, and nothing
undecidable is reported as certain.

Re-deriving "as known"
----------------------
Stored records carry closures (an event end, status ``superseded``) that a later write inferred. For
``known_at`` and ``both`` the known subset is therefore re-derived: *inferred* ends (``basis="inferred"``, as
written by LIB-02 closures) and superseded statuses are re-opened, then an inferred end is kept only if a known
record of the same (subject, attribute) takes effect at that instant. Ends the user stated (``explicit``) stay.
The engine can inject an exact re-derivation (``rederive=``, e.g. a LIB-02 replay with its cardinality
registry); ``memspine.read`` may not import ``memspine.derive`` (import contract), so this module ships its own
small calendar core. Its three-valued overlap is held equal to ``memspine.derive.temporal`` by a parity test.
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, model_validator

from memspine.schemas.durable.assertion import AssertionEnvelope
from memspine.schemas.durable.temporal import Interval, TemporalRef, TimeAxes

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

__all__ = [
    "HistoryEntry",
    "HistoryRun",
    "LiveValue",
    "TimeHit",
    "TimeView",
    "TimeViewInput",
    "TimeViewMode",
    "TimeViewResult",
    "apply",
    "temporal_score",
]

TimeViewMode = Literal["current", "true_at", "known_at", "both", "history", "range"]


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", arbitrary_types_allowed=True)


# ------------------------------------------------------------------------------------------- request


class TimeViewInput(_Frozen):
    """The time fields of a query contract (R-24), reduced to what the view needs.

    ``scope`` is the contract's reading of the question; ``as_of_valid`` may be a bare instant or a
    precision-bearing :class:`TemporalRef` ("March 2022" = month precision). ``as_of_known`` is only ever
    set by the caller. An unclear or incomplete scope falls back to ``current``.
    """

    scope: Literal["unclear", "current", "history", "as_of", "range"] = "unclear"
    as_of_valid: AwareDatetime | TemporalRef | None = None
    as_of_known: AwareDatetime | None = None
    range_start: AwareDatetime | None = None
    range_end: AwareDatetime | None = None


class TimeView(_Frozen):
    """A time-view request. Build with the classmethods or :meth:`from_contract`."""

    mode: TimeViewMode = "current"
    valid: AwareDatetime | TemporalRef | None = None  # event-time point or period (true_at, both)
    known: AwareDatetime | None = None  # knowledge time (known_at, both)
    start: AwareDatetime | None = None  # range, inclusive
    end: AwareDatetime | None = None  # range, exclusive

    @model_validator(mode="after")
    def _consistent(self) -> TimeView:
        need_valid = self.mode in ("true_at", "both")
        need_known = self.mode in ("known_at", "both")
        if need_valid != (self.valid is not None):
            raise ValueError(f"mode {self.mode!r} {'needs' if need_valid else 'forbids'} 'valid'")
        if need_known != (self.known is not None):
            raise ValueError(f"mode {self.mode!r} {'needs' if need_known else 'forbids'} 'known'")
        if self.mode == "range":
            if self.start is None and self.end is None:
                raise ValueError("range needs a start or an end")
            if self.start is not None and self.end is not None and self.end < self.start:
                raise ValueError("range end is before its start")
        elif self.start is not None or self.end is not None:
            raise ValueError(f"mode {self.mode!r} forbids a range")
        return self

    @classmethod
    def current(cls) -> TimeView:
        return cls(mode="current")

    @classmethod
    def history(cls) -> TimeView:
        return cls(mode="history")

    @classmethod
    def at_valid(cls, t: datetime | TemporalRef) -> TimeView:
        return cls(mode="true_at", valid=t)

    @classmethod
    def at_known(cls, t: datetime) -> TimeView:
        return cls(mode="known_at", known=t)

    @classmethod
    def both(cls, valid: datetime | TemporalRef, known: datetime) -> TimeView:
        return cls(mode="both", valid=valid, known=known)

    @classmethod
    def between(cls, start: datetime | None, end: datetime | None) -> TimeView:
        return cls(mode="range", start=start, end=end)

    @classmethod
    def from_contract(cls, inp: TimeViewInput) -> TimeView:
        """Pick the view from the contract's time fields (R-24). Caller ``as_of_known`` always wins an axis."""
        valid, known = inp.as_of_valid, inp.as_of_known
        if known is not None and valid is not None:
            return cls.both(valid, known)
        if known is not None:
            return cls.at_known(known)
        if valid is not None:
            return cls.at_valid(valid)
        if inp.scope == "history":
            return cls.history()
        if inp.scope == "range" and (inp.range_start is not None or inp.range_end is not None):
            return cls.between(inp.range_start, inp.range_end)
        return cls.current()  # current, unclear, or an as_of/range scope without a usable date


# ------------------------------------------------------------------------------------------- result


class TimeHit(_Frozen):
    """An assertion that stays in the result. ``uncertain_time`` means a verdict was ``None``.

    ``valid``/``known`` are the event-axis and knowledge-axis verdicts (``None`` = undecidable or not asked).
    ``assertion`` is the state as seen by the view: for ``known_at``/``both`` that is the re-derived
    as-known record. ``score`` is the temporal-leg score (``None`` = unscored).
    """

    record_id: str
    assertion: AssertionEnvelope
    window: Literal["in", "undecidable"]
    uncertain_time: bool
    valid: bool | None = None
    known: bool | None = None
    live: bool = False
    score: float | None = None


class LiveValue(_Frozen):
    subject: str
    attribute: str
    value: str
    record_id: str


class HistoryEntry(_Frozen):
    record_id: str
    value: str
    start: AwareDatetime | None
    end: AwareDatetime | None
    status: str


class HistoryRun(_Frozen):
    """The ordered values of one (subject, attribute)."""

    subject: str
    attribute: str
    entries: tuple[HistoryEntry, ...]


class TimeViewResult(_Frozen):
    view: TimeView
    hits: tuple[TimeHit, ...] = ()  # in-window and undecidable, deterministic order
    out_of_window: tuple[str, ...] = ()  # record ids definitely outside the view
    live: tuple[LiveValue, ...] = ()  # current/known_at/both: live values per (subject, attribute)
    history: tuple[
        HistoryRun, ...
    ] = ()  # history mode: all keys; current mode: keys whose value changed

    @property
    def uncertain(self) -> tuple[TimeHit, ...]:
        return tuple(h for h in self.hits if h.uncertain_time)


# ------------------------------------------------------------------------------------------- calendar core
# Local, trimmed copy of the LIB-01 period / three-valued overlap logic (see module docstring).

_MICRO = timedelta(microseconds=1)


@dataclass(frozen=True)
class _Span:
    """A half-open time range ``[lo, hi)``; ``None`` = unbounded on that side."""

    lo: datetime | None
    hi: datetime | None


@dataclass(frozen=True)
class _Fuzzy:
    narrow: _Span
    wide: _Span


def _midnight(d: date, tz: tzinfo | None) -> datetime:
    return datetime.combine(d, time(0), tzinfo=tz)


def _abs_add(dt: datetime, delta: timedelta) -> datetime:
    return (dt.astimezone(UTC) + delta).astimezone(dt.tzinfo)


def _add_months(d: date, k: int) -> date:
    total = d.year * 12 + (d.month - 1) + k
    year, month0 = divmod(total, 12)
    if not 1 <= year <= 9999:
        raise OverflowError("year out of range")
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _floor(dt: datetime, precision: str) -> datetime:
    if precision == "instant":
        return dt
    if precision == "minute":
        return dt.replace(second=0, microsecond=0)
    if precision == "hour":
        return dt.replace(minute=0, second=0, microsecond=0)
    d = dt.date()
    if precision == "day":
        return _midnight(d, dt.tzinfo)
    if precision == "week":
        return _midnight(d - timedelta(days=d.weekday()), dt.tzinfo)
    if precision == "month":
        return _midnight(d.replace(day=1), dt.tzinfo)
    if precision == "season":  # meteorological: Dec/Mar/Jun/Sep starts
        start_month = {12: 12, 1: 12, 2: 12}.get(d.month, d.month - (d.month - 3) % 3)
        year = d.year - 1 if d.month in (1, 2) else d.year
        return _midnight(date(year, start_month, 1), dt.tzinfo)
    if precision == "year":
        return _midnight(date(d.year, 1, 1), dt.tzinfo)
    if precision == "decade":
        return _midnight(date(d.year // 10 * 10 or 1, 1, 1), dt.tzinfo)
    raise ValueError(f"precision {precision!r} has no period")


def _shift(floor: datetime, precision: str, k: int) -> datetime:
    if precision == "instant":
        return _abs_add(floor, _MICRO * k)
    if precision == "minute":
        return _abs_add(floor, timedelta(minutes=k))
    if precision == "hour":
        return _abs_add(floor, timedelta(hours=k))
    d = floor.date()
    if precision == "day":
        nd = d + timedelta(days=k)
    elif precision == "week":
        nd = d + timedelta(days=7 * k)
    elif precision == "month":
        nd = _add_months(d, k)
    elif precision == "season":
        nd = _add_months(d, 3 * k)
    elif precision == "year":
        nd = _add_months(d, 12 * k)
    elif precision == "decade":
        nd = _add_months(d, 120 * k)
    else:
        raise ValueError(f"precision {precision!r} has no period")
    return _midnight(nd, floor.tzinfo)


def _ref_bounds(ref: TemporalRef) -> tuple[datetime, datetime, datetime, datetime] | None:
    """(lo, hi, lo_wide, hi_wide); wide differs only if the ref is approximate (one unit each side)."""
    if ref.at is None or ref.precision == "unknown":
        return None
    try:
        lo = _floor(ref.at, ref.precision)
        hi = _shift(lo, ref.precision, 1)
        if not ref.approximate:
            return lo, hi, lo, hi
        return lo, hi, _shift(lo, ref.precision, -1), _shift(lo, ref.precision, 2)
    except (ValueError, OverflowError):
        return None


def _event_interval(ref: TemporalRef) -> Interval | None:
    if ref.at is None or ref.precision == "unknown":
        return None
    if ref.basis == "relation" and ref.relation is not None:
        if ref.relation.relation == "after":
            return Interval(start=ref, end=None)
        if ref.relation.relation == "before":
            return Interval(start=None, end=ref)
    return Interval(start=ref, end=ref)


_Operand = TemporalRef | Interval | _Span | datetime


def _fuzzy(x: _Operand) -> _Fuzzy | None:
    if isinstance(x, _Span):
        return _Fuzzy(x, x)
    if isinstance(x, datetime):
        s = _Span(x, x + _MICRO)
        return _Fuzzy(s, s)
    if isinstance(x, Interval):
        lo = lo_w = hi = hi_w = None
        if x.start is not None:
            b = _ref_bounds(x.start)
            if b is None:
                return None
            lo, _, lo_w, _ = b
        if x.end is not None:
            b = _ref_bounds(x.end)
            if b is None:
                return None
            _, hi, _, hi_w = b
        return _Fuzzy(_Span(lo, hi), _Span(lo_w, hi_w))
    iv = _event_interval(x)
    if iv is None:
        return None
    f = _fuzzy(iv)
    if (
        f is not None
        and x.basis == "relation"
        and x.relation is not None
        and x.relation.relation in ("before", "after")
    ):  # an exact boundary: do not fuzz it
        return _Fuzzy(f.narrow, f.narrow)
    return f


def _overlap(a: _Span, b: _Span) -> bool:
    return (a.lo is None or b.hi is None or a.lo < b.hi) and (
        b.lo is None or a.hi is None or b.lo < a.hi
    )


def _overlaps(a: _Operand, b: _Operand) -> bool | None:
    """Three-valued: True if they overlap at the narrow reading, False if not even widened, else None."""
    fa, fb = _fuzzy(a), _fuzzy(b)
    if fa is None or fb is None:
        return None
    if _overlap(fa.narrow, fb.narrow):
        return True
    return None if _overlap(fa.wide, fb.wide) else False


def _true_at(axes: TimeAxes, t: _Operand) -> bool | None:
    """Answer "what was true on t": event time only; None without a resolved event time."""
    if axes.event is None:
        return None
    return _overlaps(axes.event, t)


def _known_at(axes: TimeAxes, t: datetime) -> bool | None:
    """Answer "what did we know on t": every available source/recorded stamp is <= t; None if none."""
    present = [s for s in (axes.source_time, axes.recorded_at) if s is not None]
    if not present:
        return None
    return all(s <= t for s in present)


# ------------------------------------------------------------------------------------------- record keys

_MIN_AWARE = datetime(1, 1, 1, tzinfo=UTC)


def _record_id(e: AssertionEnvelope) -> str:
    return e.record_id or e.occurrence_id


def _start_at(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    return ev.start.at if ev is not None and ev.start is not None else None


def _end_at(e: AssertionEnvelope) -> datetime | None:
    ev = e.time.event
    return ev.end.at if ev is not None and ev.end is not None else None


def _effective(e: AssertionEnvelope) -> datetime | None:
    """Event start, else source time, else recorded time, else event end (same rule as LIB-02)."""
    return _start_at(e) or e.time.source_time or e.time.recorded_at or _end_at(e)


def _order_key(e: AssertionEnvelope) -> tuple[datetime, datetime, datetime, str]:
    """Event time, source time, recorded time, occurrence id: arrival order is irrelevant."""
    return (
        _effective(e) or _MIN_AWARE,
        e.time.source_time or _MIN_AWARE,
        e.time.recorded_at or _MIN_AWARE,
        e.occurrence_id,
    )


# ------------------------------------------------------------------------------------------- helpers


def _subject(e: AssertionEnvelope) -> str:
    s = e.subject
    return "" if s is None else (s.entity_id or s.surface.strip().casefold())


def _attr(e: AssertionEnvelope) -> str:
    return "_".join((e.attribute or e.kind).casefold().split())


def _value(e: AssertionEnvelope) -> str:
    v = e.value if e.value is not None else e.text
    return str(v)


def _is_live(e: AssertionEnvelope) -> bool:
    ev = e.time.event
    closed = ev is not None and ev.end is not None and ev.end.at is not None
    return (
        e.status in ("active", "contested") and e.assessment.modality == "asserted" and not closed
    )


def _is_value(e: AssertionEnvelope) -> bool:
    return e.status not in ("hidden", "tentative") and e.assessment.modality == "asserted"


def _arrival_key(e: AssertionEnvelope) -> tuple[datetime, datetime, str]:
    a = e.time.recorded_at or e.time.source_time
    s = e.time.source_time
    return (
        a or _MIN_AWARE,
        s or _MIN_AWARE,
        e.occurrence_id,
    )


def _reopen(e: AssertionEnvelope) -> AssertionEnvelope:
    """Drop what a later write inferred: an ``inferred`` event end and a superseded status."""
    ev = e.time.event
    upd: dict[str, object] = {}
    if ev is not None and ev.end is not None and ev.end.basis == "inferred":
        new_ev = Interval(start=ev.start) if ev.start is not None else None
        upd["time"] = e.time.model_copy(update={"event": new_ev})
    if e.status == "superseded":
        upd["status"] = "active"
    return e.model_copy(update=upd) if upd else e


def _closer_known(e: AssertionEnvelope, known: Sequence[AssertionEnvelope]) -> bool:
    """Is some known record of the same (subject, attribute) taking effect at ``e``'s inferred end?"""
    end = _end_at(e)
    key = (_subject(e), _attr(e))
    return any(
        _record_id(o) != _record_id(e) and (_subject(o), _attr(o)) == key and _effective(o) == end
        for o in known
    )


def _as_known(
    known_records: Iterable[AssertionEnvelope],
    rederive: Callable[[list[AssertionEnvelope]], Sequence[AssertionEnvelope]] | None,
) -> list[AssertionEnvelope]:
    """The store as it stood once exactly ``known_records`` were known.

    With ``rederive`` (e.g. a LIB-02 replay) the re-opened records are handed to it in arrival order.
    Without it, an inferred closure is kept only while a known record that could have caused it exists.
    """
    recs = sorted(known_records, key=_arrival_key)
    if rederive is not None:
        return list(rederive([_reopen(e) for e in recs]))
    out: list[AssertionEnvelope] = []
    for e in recs:
        ev = e.time.event
        inferred = ev is not None and ev.end is not None and ev.end.basis == "inferred"
        out.append(e if not inferred or _closer_known(e, recs) else _reopen(e))
    return out


def _target_span(view: TimeView) -> _Span | None:
    """The event-time period the temporal leg should match, or ``None`` (unscored view)."""
    if view.mode in ("true_at", "both"):
        f = _fuzzy(view.valid)  # type: ignore[arg-type]
        return None if f is None else f.narrow
    if view.mode == "range":
        return _Span(view.start, view.end)
    return None


def _coverage(n: _Span, t: _Span) -> float:
    """Share of the target ``t`` covered by ``n`` (1.0 for an unbounded or instant target that overlaps)."""
    if t.lo is None or t.hi is None or (t.hi - t.lo) <= _MICRO:
        return 1.0
    lo = t.lo if n.lo is None else max(n.lo, t.lo)
    hi = t.hi if n.hi is None else min(n.hi, t.hi)
    if hi <= lo:
        return 0.0
    return min(1.0, (hi - lo) / (t.hi - t.lo))


def temporal_score(assertion: AssertionEnvelope, view: TimeView) -> float | None:
    """Temporal-leg score in [0, 1] from event-interval overlap with the view's target period.

    * ``None`` (unscored, never 0) when the view has no event-time target (current, known_at, history), when the
      assertion has no resolved event time, or when the target cannot be resolved.
    * ``1.0 * coverage`` when the event interval overlaps the target for certain; coverage is the share of the
      target period the event covers (1.0 for an instant). A coarse event (``2022``) fully covering "March
      2022" therefore scores 1.0; coarseness is not penalised, it is reported as ``uncertain_time`` only when
      the answer is undecidable.
    * ``0.5 * coverage`` when only the precision-aware widening of an *approximate* date reaches the target
      (approximate dates are widened by one unit of their precision each side).
    * ``0.0`` when even the widened interval misses the target.
    """
    ev = assertion.time.event
    t = _target_span(view)
    if ev is None or t is None:
        return None
    f = _fuzzy(ev)
    if f is None:
        return None
    if _overlap(f.narrow, t):
        return _coverage(f.narrow, t)
    if _overlap(f.wide, t):
        return 0.5 * _coverage(f.wide, t)
    return 0.0


# ------------------------------------------------------------------------------------------- apply


def _valid_verdict(e: AssertionEnvelope, view: TimeView) -> bool | None:
    if view.mode in ("true_at", "both"):
        return _true_at(e.time, view.valid)  # type: ignore[arg-type]
    if view.mode == "range":
        if e.time.event is None:
            return None
        return _overlaps(e.time.event, _Span(view.start, view.end))
    return None


def _history_runs(
    records: Iterable[AssertionEnvelope], *, only_changed: bool
) -> tuple[HistoryRun, ...]:
    groups: dict[tuple[str, str], list[AssertionEnvelope]] = {}
    for e in records:
        if _is_value(e):
            groups.setdefault((_subject(e), _attr(e)), []).append(e)
    runs: list[HistoryRun] = []
    for (subj, attr), recs in sorted(groups.items()):
        recs.sort(key=_order_key)
        if only_changed and len(recs) < 2:
            continue
        entries = tuple(
            HistoryEntry(
                record_id=_record_id(e),
                value=_value(e),
                start=e.time.event.start.at if e.time.event and e.time.event.start else None,
                end=e.time.event.end.at if e.time.event and e.time.event.end else None,
                status=e.status,
            )
            for e in recs
        )
        runs.append(HistoryRun(subject=subj, attribute=attr, entries=entries))
    return tuple(runs)


def _live_values(records: Iterable[AssertionEnvelope]) -> tuple[LiveValue, ...]:
    rows = [
        LiveValue(subject=_subject(e), attribute=_attr(e), value=_value(e), record_id=_record_id(e))
        for e in records
        if _is_live(e)
    ]
    return tuple(sorted(rows, key=lambda r: (r.subject, r.attribute, r.value, r.record_id)))


def apply(
    view: TimeView,
    assertions: Iterable[AssertionEnvelope],
    rederive: Callable[[list[AssertionEnvelope]], Sequence[AssertionEnvelope]] | None = None,
) -> TimeViewResult:
    """Filter and annotate ``assertions`` under ``view``. Deterministic; independent of input order."""
    items = list(assertions)
    pool = sorted(
        (e for e in items if e.status != "hidden"), key=lambda e: (_order_key(e), _record_id(e))
    )
    res = _apply(view, pool, rederive)
    hidden = tuple(_record_id(e) for e in items if e.status == "hidden")
    if not hidden:
        return res
    return res.model_copy(update={"out_of_window": tuple(sorted({*res.out_of_window, *hidden}))})


def _apply(
    view: TimeView,
    pool: list[AssertionEnvelope],
    rederive: Callable[[list[AssertionEnvelope]], Sequence[AssertionEnvelope]] | None,
) -> TimeViewResult:
    out: list[str] = []
    state = pool  # records as the view sees them
    known: dict[str, bool | None] = {}

    if view.mode in ("known_at", "both"):
        assert view.known is not None
        known = {_record_id(e): _known_at(e.time, view.known) for e in pool}
        replayed = {
            _record_id(e): e for e in _as_known([e for e in pool if known[_record_id(e)]], rederive)
        }
        # undecidable-knowledge records are kept as stored (they never entered the replay)
        state = [replayed.get(_record_id(e), e) if known[_record_id(e)] else e for e in pool]
        state = sorted(state, key=lambda e: (_order_key(e), _record_id(e)))
        out.extend(_record_id(e) for e in pool if known[_record_id(e)] is False)
        state = [e for e in state if known.get(_record_id(e)) is not False]

    hits: list[TimeHit] = []
    for e in state:
        rid = _record_id(e)
        k = known.get(rid) if known else None
        if view.mode == "current":
            if not _is_live(e):
                out.append(rid)
                continue
            v: bool | None = None
            decided: bool | None = True
        elif view.mode == "history":
            v, decided = None, True
        elif view.mode == "known_at":
            v, decided = None, k
        else:  # true_at, both, range
            v = _valid_verdict(e, view)
            decided = v if view.mode != "both" else _and3(k, v)
        if decided is False:
            out.append(rid)
            continue
        uncertain = decided is None
        hits.append(
            TimeHit(
                record_id=rid,
                assertion=e,
                window="undecidable" if uncertain else "in",
                uncertain_time=uncertain,
                valid=v,
                known=k,
                live=_is_live(e),
                score=temporal_score(e, view),
            )
        )

    kept = [h.assertion for h in hits]
    live = (
        _live_values([h.assertion for h in hits if not h.uncertain_time])
        if view.mode in ("current", "known_at", "both")
        else ()
    )
    if view.mode == "history":
        history = _history_runs(kept, only_changed=False)
    elif view.mode == "current":
        history = _history_runs(pool, only_changed=True)
    else:
        history = ()
    return TimeViewResult(
        view=view,
        hits=tuple(hits),
        out_of_window=tuple(sorted(set(out))),
        live=live,
        history=history,
    )


def _and3(a: bool | None, b: bool | None) -> bool | None:
    if a is False or b is False:
        return False
    if a is None or b is None:
        return None
    return True
