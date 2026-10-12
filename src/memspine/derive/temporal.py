# ruff: noqa: E501
"""Temporal anchoring (LIB-01): the S6 time model as pure functions over ``memspine.schemas`` types.

Pure and deterministic: no I/O, no clock reads ("now" is always an argument), no model calls. The model
proposes text plus a parsed candidate (:class:`TemporalCandidate`); this module computes anchor, basis and
precision (S2-1, S6-2). Wiring into the engine is W2-14; nothing here is imported by the engine.

Rules encoded
-------------
* Never invent a date (S6-2). Output precision is never finer than the input supports: ``2023-04`` stays
  month precision, ``last week`` is a week-precision ref, an undated fact has no event time.
* A relative phrase with no source time is kept as an *unanchored* phrase: ``basis="none"``, no date, the
  phrase kept in ``text``. (``basis="relation"`` needs a target assertion id, which a bare phrase lacks.)
* ``TemporalRef.at`` is the start of the period at ``precision``. A ref therefore stands for the half-open
  period ``[at, next(at))``. In an :class:`Interval` the start ref contributes the start of its period and the
  end ref the end of its period (period-inclusive ends: ``2020``..``2022`` covers all of 2022). ``None`` ends
  are open (unbounded).
* Weeks are ISO (Monday start). Seasons are meteorological (three whole months). A season is named by the
  year it *begins* in (north: winter 2023 = Dec 2023 - Feb 2024). The hemisphere only changes season names.
* Three-valued logic: comparisons return ``True``, ``False`` or ``None`` (cannot decide). A ref with no
  resolved date, or an unresolved interval end, gives ``None``. An ``approximate`` ref is treated as fuzzy by
  one unit of its precision on each side: the answer is definite only if it holds across that whole fuzz.
* Local calendar arithmetic is done in the declared zone (``tz``); a naive datetime is read in that zone.
"""

from __future__ import annotations

import calendar
import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta, tzinfo
from typing import TYPE_CHECKING, Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

from memspine.schemas.durable.temporal import (
    Duration,
    DurationUnit,
    Interval,
    TemporalRef,
    TemporalRelation,
    TimeAxes,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

__all__ = [
    "Hemisphere",
    "Span",
    "TemporalCandidate",
    "add_duration",
    "after",
    "anchor",
    "before",
    "candidate_event_interval",
    "contains",
    "duration_interval",
    "event_interval",
    "is_approximate_phrase",
    "known_at",
    "normalise_relative_phrase",
    "overlaps",
    "period",
    "precision_interval",
    "resolve_relation",
    "season_start_month",
    "true_at",
]

Hemisphere = Literal["north", "south"]
Season = Literal["winter", "spring", "summer", "autumn"]
CandidateKind = Literal["absolute", "partial", "relative", "duration", "relation", "none"]
PartialKind = Literal["year", "year_month", "season", "decade"]
RelForm = Literal["ago", "ahead", "last", "next", "this"]
RelUnit = Literal["minute", "hour", "day", "week", "month", "season", "year", "weekday", "weekend"]

_ORDER: tuple[str, ...] = (
    "instant",
    "minute",
    "hour",
    "day",
    "week",
    "month",
    "season",
    "year",
    "decade",
)
_ABSOLUTE_TIME_PRECISIONS = ("instant", "minute", "hour")
_SEASON_NORTH: dict[str, int] = {"winter": 12, "spring": 3, "summer": 6, "autumn": 9}
_SEASON_SOUTH: dict[str, int] = {"summer": 12, "autumn": 3, "winter": 6, "spring": 9}
_MICRO = timedelta(microseconds=1)
_UNIT_PRECISION: dict[str, str] = {
    "second": "instant",
    "minute": "minute",
    "hour": "hour",
    "day": "day",
    "week": "day",
    "month": "month",
    "year": "year",
}


# ---------------------------------------------------------------------------------------------------------
# candidate (the typed input the model's parse is turned into)
# ---------------------------------------------------------------------------------------------------------


class TemporalCandidate(BaseModel):
    """A parsed temporal phrase. The model supplies it; :func:`anchor` resolves it.

    Kinds and the fields each uses:

    * ``absolute``: exactly one of ``date_value`` (day precision) or ``datetime_value`` (``time_precision``
      one of instant/minute/hour; default instant if it has seconds, else minute).
    * ``partial``: ``partial`` = year (``year``), year_month (``year``, ``month``), season (``year``,
      ``season``), decade (``year`` = first year, a multiple of 10).
    * ``relative``: ``rel`` + ``unit``. ``ago``/``ahead`` need ``n`` >= 1 and a clock/calendar unit
      (yesterday = ago/day/1, tomorrow = ahead/day/1). ``last``/``next``/``this`` take day, week, month,
      season, year, weekend, or weekday (with ``weekday`` 0 = Monday). "last/next <weekday>" is the nearest
      such weekday strictly before/after the source date; "this <weekday>" is the one in the source's ISO
      week. "this weekend" is the coming Saturday-Sunday (the current one on a Saturday or Sunday).
    * ``duration``: ``duration_value`` + ``duration_unit``; optional ``anchor_candidate`` (what the duration
      is anchored to) and ``anchor_role`` (``end``: "for 3 years until 2024"; ``start``: "for 3 years from
      2020").
    * ``relation``: ``relation`` (before/after/during an assertion id).
    * ``none``: no temporal phrase.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: CandidateKind
    text: str | None = None
    approximate: bool = False

    date_value: date | None = None
    datetime_value: datetime | None = None
    time_precision: Literal["instant", "minute", "hour"] | None = None

    partial: PartialKind | None = None
    year: int | None = Field(default=None, ge=1, le=9999)
    month: int | None = Field(default=None, ge=1, le=12)
    season: Season | None = None

    rel: RelForm | None = None
    unit: RelUnit | None = None
    n: int | None = Field(default=None, ge=1, le=100_000)
    weekday: int | None = Field(default=None, ge=0, le=6)

    duration_value: float | None = Field(default=None, gt=0)
    duration_unit: DurationUnit | None = None
    anchor_candidate: TemporalCandidate | None = None
    anchor_role: Literal["start", "end"] = "end"

    relation: TemporalRelation | None = None

    @model_validator(mode="after")
    def _shape(self) -> TemporalCandidate:
        k = self.kind
        if k == "absolute":
            if (self.date_value is None) == (self.datetime_value is None):
                raise ValueError("absolute needs exactly one of date_value / datetime_value")
            if self.time_precision is not None and self.datetime_value is None:
                raise ValueError("time_precision only applies to datetime_value")
        elif k == "partial":
            if self.partial is None or self.year is None:
                raise ValueError("partial needs 'partial' and 'year'")
            if self.partial == "year_month" and self.month is None:
                raise ValueError("year_month needs 'month'")
            if self.partial == "season" and self.season is None:
                raise ValueError("season needs 'season'")
            if self.partial == "decade" and self.year % 10 != 0:
                raise ValueError("decade 'year' must be the first year (a multiple of 10)")
        elif k == "relative":
            if self.rel is None or self.unit is None:
                raise ValueError("relative needs 'rel' and 'unit'")
            if self.rel in ("ago", "ahead"):
                if self.n is None:
                    raise ValueError("ago/ahead need 'n'")
                if self.unit in ("weekday", "weekend", "season"):
                    raise ValueError(f"{self.rel} does not combine with unit {self.unit!r}")
            else:
                if self.unit in ("minute", "hour"):
                    raise ValueError(f"{self.rel} does not combine with unit {self.unit!r}")
            if self.unit == "weekday" and self.weekday is None:
                raise ValueError("unit 'weekday' needs 'weekday'")
        elif k == "duration":
            if self.duration_value is None or self.duration_unit is None:
                raise ValueError("duration needs duration_value and duration_unit")
        elif k == "relation":
            if self.relation is None:
                raise ValueError("relation needs 'relation'")
        return self


TemporalCandidate.model_rebuild()


# ---------------------------------------------------------------------------------------------------------
# zones and calendar primitives
# ---------------------------------------------------------------------------------------------------------


def _zone(tz: tzinfo | str) -> tzinfo:
    return ZoneInfo(tz) if isinstance(tz, str) else tz


def _aware(dt: datetime, tz: tzinfo) -> datetime:
    """``dt`` in zone ``tz`` (a naive value is read as local time in ``tz``)."""
    return dt.replace(tzinfo=tz) if dt.tzinfo is None else dt.astimezone(tz)


def _midnight(d: date, tz: tzinfo | None) -> datetime:
    return datetime.combine(d, time(0), tzinfo=tz)


def _abs_add(dt: datetime, delta: timedelta) -> datetime:
    """Elapsed-time addition (DST-safe), result in ``dt``'s zone."""
    return (dt.astimezone(UTC) + delta).astimezone(dt.tzinfo)


def _add_months(d: date, k: int) -> date:
    total = d.year * 12 + (d.month - 1) + k
    year, month0 = divmod(total, 12)
    if not 1 <= year <= 9999:
        raise OverflowError("year out of range")
    month = month0 + 1
    return date(year, month, min(d.day, calendar.monthrange(year, month)[1]))


def _pidx(precision: str) -> int:
    return _ORDER.index(precision)


def _floor(dt: datetime, precision: str) -> datetime:
    """Start of the ``precision`` period containing ``dt`` (in ``dt``'s own zone)."""
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
    if precision == "season":
        # season starts are Dec/Mar/Jun/Sep in both hemispheres
        start_month = {12: 12, 1: 12, 2: 12}.get(d.month, d.month - (d.month - 3) % 3)
        year = d.year - 1 if d.month in (1, 2) else d.year
        return _midnight(date(year, start_month, 1), dt.tzinfo)
    if precision == "year":
        return _midnight(date(d.year, 1, 1), dt.tzinfo)
    if precision == "decade":
        return _midnight(date(d.year // 10 * 10 or 1, 1, 1), dt.tzinfo)
    raise ValueError(f"precision {precision!r} has no period")


def _shift(floor: datetime, precision: str, k: int) -> datetime:
    """``floor`` (already a period start) moved by ``k`` periods of ``precision``."""
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


def season_start_month(season: str, hemisphere: Hemisphere = "north") -> int:
    """First month (1-12) of a meteorological season. Raises ``ValueError`` for an unknown name."""
    table = _SEASON_NORTH if hemisphere == "north" else _SEASON_SOUTH
    if season not in table:
        raise ValueError(f"unknown season {season!r}")
    return table[season]


def precision_interval(
    at: datetime, precision: str, *, tz: tzinfo | str | None = None
) -> tuple[datetime, datetime]:
    """The half-open period ``[start, end)`` of ``precision`` that contains ``at``.

    ``precision``: instant (one microsecond), minute, hour, day, week (ISO, Monday start), month, season
    (meteorological: Dec/Mar/Jun/Sep starts; identical in both hemispheres, which differ only in names, see
    :func:`season_start_month`), year, decade. ``unknown`` raises ``ValueError``. ``at`` is read in ``tz``
    (default: its own zone; a naive ``at`` needs ``tz``).
    """
    if at.tzinfo is None:
        if tz is None:
            raise ValueError("naive datetime needs tz")
        at = _aware(at, _zone(tz))
    elif tz is not None:
        at = at.astimezone(_zone(tz))
    lo = _floor(at, precision)
    return lo, _shift(lo, precision, 1)


# ---------------------------------------------------------------------------------------------------------
# spans and three-valued comparisons
# ---------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """A half-open time range ``[lo, hi)``; ``None`` = unbounded on that side."""

    lo: datetime | None
    hi: datetime | None

    def __post_init__(self) -> None:
        for v in (self.lo, self.hi):
            if v is not None and v.tzinfo is None:
                raise ValueError("Span bounds must be timezone-aware")
        if self.lo is not None and self.hi is not None and self.hi < self.lo:
            raise ValueError("Span hi before lo")


@dataclass(frozen=True)
class _Fuzzy:
    narrow: Span
    wide: Span


Operand = TemporalRef | Interval | Span | datetime


def period(ref: TemporalRef, *, tz: tzinfo | str | None = None) -> Span | None:
    """The period a resolved ref stands for, or ``None`` if it has no date or precision."""
    if ref.at is None or ref.precision == "unknown":
        return None
    try:
        lo, hi = precision_interval(ref.at, ref.precision, tz=tz)
    except (ValueError, OverflowError):
        return None
    return Span(lo, hi)


def _ref_bounds(ref: TemporalRef) -> tuple[datetime, datetime, datetime, datetime] | None:
    """(lo, hi, lo_wide, hi_wide) of a resolved ref; wide differs only if approximate."""
    if ref.at is None or ref.precision == "unknown":
        return None
    try:
        lo, hi = precision_interval(ref.at, ref.precision)
        if not ref.approximate:
            return lo, hi, lo, hi
        return lo, hi, _shift(lo, ref.precision, -1), _shift(lo, ref.precision, 2)
    except (ValueError, OverflowError):
        return None


def _fuzzy(x: Operand) -> _Fuzzy | None:
    if isinstance(x, Span):
        return _Fuzzy(x, x)
    if isinstance(x, datetime):
        if x.tzinfo is None:
            raise ValueError("instant operand must be timezone-aware")
        s = Span(x, x + _MICRO)
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
        return _Fuzzy(Span(lo, hi), Span(lo_w, hi_w))
    iv = event_interval(x)  # TemporalRef
    if iv is None:
        return None
    if (
        x.basis == "relation"
        and x.relation is not None
        and x.relation.relation in ("before", "after")
    ):
        # a before/after bound is an exact boundary: do not fuzz it
        f = _fuzzy(iv)
        return None if f is None else _Fuzzy(f.narrow, f.narrow)
    return _fuzzy(iv)


def _overlap(a: Span, b: Span) -> bool:
    return (a.lo is None or b.hi is None or a.lo < b.hi) and (
        b.lo is None or a.hi is None or b.lo < a.hi
    )


def _contain(a: Span, b: Span) -> bool:
    lo_ok = a.lo is None or (b.lo is not None and a.lo <= b.lo)
    hi_ok = a.hi is None or (b.hi is not None and b.hi <= a.hi)
    return lo_ok and hi_ok


def _before(a: Span, b: Span) -> bool:
    return a.hi is not None and b.lo is not None and a.hi <= b.lo


def _decide(pessimistic: bool, optimistic: bool) -> bool | None:
    if pessimistic:
        return True
    if not optimistic:
        return False
    return None


def overlaps(a: Operand, b: Operand) -> bool | None:
    """Do the two share any time? ``None`` if either is unresolved or approximate in a way that matters."""
    fa, fb = _fuzzy(a), _fuzzy(b)
    if fa is None or fb is None:
        return None
    return _decide(_overlap(fa.narrow, fb.narrow), _overlap(fa.wide, fb.wide))


def contains(outer: Operand, inner: Operand) -> bool | None:
    """Is ``inner`` entirely within ``outer``? An open end of ``outer`` contains anything; ``inner`` open
    where ``outer`` is closed is not contained. ``None`` if undecidable."""
    fo, fi = _fuzzy(outer), _fuzzy(inner)
    if fo is None or fi is None:
        return None
    return _decide(_contain(fo.narrow, fi.wide), _contain(fo.wide, fi.narrow))


def before(a: Operand, b: Operand) -> bool | None:
    """Is ``a`` entirely before ``b`` (``a`` ends no later than ``b`` starts)? ``None`` if undecidable."""
    fa, fb = _fuzzy(a), _fuzzy(b)
    if fa is None or fb is None:
        return None
    return _decide(_before(fa.wide, fb.wide), _before(fa.narrow, fb.narrow))


def after(a: Operand, b: Operand) -> bool | None:
    """Is ``a`` entirely after ``b``? Mirror of :func:`before`."""
    return before(b, a)


# ---------------------------------------------------------------------------------------------------------
# durations
# ---------------------------------------------------------------------------------------------------------


def _int_value(value: float) -> int | None:
    return int(value) if float(value).is_integer() else None


def add_duration(dt: datetime, duration: Duration, *, sign: int = 1) -> datetime | None:
    """``dt`` plus (``sign=1``) or minus (``sign=-1``) a duration; calendar-aware (months and years clamp the
    day, days/weeks are local-calendar days, shorter units are elapsed time). ``None`` if the duration is
    not a whole number of its unit or the result is out of range."""
    if dt.tzinfo is None:
        raise ValueError("dt must be timezone-aware")
    n = _int_value(duration.value)
    if n is None or sign not in (1, -1):
        return None
    k = n * sign
    try:
        u = duration.unit
        if u == "year":
            return _midnight_keep(dt, _add_months(dt.date(), 12 * k))
        if u == "month":
            return _midnight_keep(dt, _add_months(dt.date(), k))
        if u in ("day", "week"):
            return _midnight_keep(dt, dt.date() + timedelta(days=k * (7 if u == "week" else 1)))
        step = {"hour": timedelta(hours=1), "minute": timedelta(minutes=1)}.get(
            u, timedelta(seconds=1)
        )
        return _abs_add(dt, step * k)
    except (OverflowError, ValueError):
        return None


def _midnight_keep(dt: datetime, d: date) -> datetime:
    """Date ``d`` at ``dt``'s wall-clock time and zone."""
    return datetime.combine(d, dt.timetz().replace(tzinfo=None), tzinfo=dt.tzinfo)


def _finer(p1: str, p2: str) -> str:
    return p1 if _pidx(p1) <= _pidx(p2) else p2


def duration_interval(
    duration: Duration,
    anchor_ref: TemporalRef | None = None,
    *,
    role: Literal["start", "end"] = "end",
) -> Interval | None:
    """Turn a duration plus an anchor into an interval, else ``None`` (it stays a duration).

    ``role="end"``: the anchor is where it ended ("for three years until 2024" -> 2022..2024, period-
    inclusive). ``role="start"``: the anchor is where it began. ``anchor_ref`` defaults to
    ``duration.anchor`` taken as an exact instant. The computed end carries ``basis="inferred"`` and the
    duration; its precision is the finer of the anchor's and the duration unit's (a year ending in 2024
    minus 5 days is day precision), never finer than that.
    """
    if anchor_ref is None:
        if duration.anchor is None:
            return None
        anchor_ref = TemporalRef(at=duration.anchor, precision="instant", basis="explicit")
    if anchor_ref.at is None or anchor_ref.precision == "unknown":
        return None
    if _int_value(duration.value) is None:
        return None
    pa = anchor_ref.precision
    p = _finer(pa, _UNIT_PRECISION[duration.unit])
    try:
        lo, hi = precision_interval(anchor_ref.at, pa)
        if role == "end":
            boundary = anchor_ref.at if pa == "instant" else hi
            far = add_duration(boundary, duration, sign=-1)
            if far is None:
                return None
            ref = _computed(_floor(far, p), p, anchor_ref, duration)
            end_ref = anchor_ref
            if p != pa:
                # finer than the anchor: its last period at the finer precision, so ends stay ordered
                end_ref = TemporalRef(
                    at=_shift(hi, p, -1),
                    precision=p,  # type: ignore[arg-type]
                    approximate=anchor_ref.approximate,
                    basis="inferred",
                )
            return Interval(start=ref, end=end_ref)
        far = add_duration(lo, duration, sign=1)
        if far is None:
            return None
        last = far if p == "instant" else _shift(_floor(far, p), p, -1)
        ref = _computed(last, p, anchor_ref, duration)
        return Interval(start=anchor_ref, end=ref)
    except (ValueError, OverflowError):
        return None


def _computed(at: datetime, p: str, anchor_ref: TemporalRef, duration: Duration) -> TemporalRef:
    return TemporalRef(
        at=at,
        precision=p,  # type: ignore[arg-type]
        approximate=anchor_ref.approximate,
        basis="inferred",
        duration=Duration(value=duration.value, unit=duration.unit, anchor=anchor_ref.at),
    )


# ---------------------------------------------------------------------------------------------------------
# relations
# ---------------------------------------------------------------------------------------------------------


def resolve_relation(rel: TemporalRelation, other: TemporalRef) -> TemporalRef | None:
    """Resolve "before/after/during <other>" against the other assertion's time, or ``None``.

    Only when ``other`` is anchored: it needs a resolved date and precision, and must not itself be a bare
    before/after bound. The result has ``basis="relation"``, carries ``rel`` and ``anchor=other.at``:

    * ``during``: the other's period (same ``at``, precision and approximate flag).
    * ``after``: the first period after the other (``at`` = end of the other's period), ``approximate=True``.
    * ``before``: the last period before the other, ``approximate=True``.

    For before/after the date is a *boundary*, not when it happened. :func:`event_interval` and the
    comparison functions read such a ref as an open-ended interval; do not show its ``at`` as the event date.
    """
    if other.at is None or other.precision == "unknown" or other.basis == "none":
        return None
    if (
        other.basis == "relation"
        and other.relation is not None
        and other.relation.relation != "during"
    ):
        return None
    try:
        lo, hi = precision_interval(other.at, other.precision)
        if rel.relation == "during":
            at, approx = lo, other.approximate
        elif rel.relation == "after":
            at, approx = hi, True
        else:
            at, approx = _shift(lo, other.precision, -1), True
        return TemporalRef(
            at=at,
            precision=other.precision,
            approximate=approx,
            basis="relation",
            anchor=other.at,
            relation=rel,
        )
    except (ValueError, OverflowError):
        return None


def event_interval(ref: TemporalRef) -> Interval | None:
    """The event-time interval a ref stands for, or ``None`` when it has no resolved date.

    A plain dated ref is its own period (start = end = the ref, period-inclusive). A resolved ``after``
    relation is open to the future, ``before`` open to the past; ``during`` is the period.
    """
    if ref.at is None or ref.precision == "unknown":
        return None
    if ref.basis == "relation" and ref.relation is not None:
        if ref.relation.relation == "after":
            return Interval(start=ref, end=None)
        if ref.relation.relation == "before":
            return Interval(start=None, end=ref)
    return Interval(start=ref, end=ref)


# ---------------------------------------------------------------------------------------------------------
# approximate markers
# ---------------------------------------------------------------------------------------------------------

_APPROX_WORDS = frozenset(
    {
        "about", "around", "circa", "approximately", "approx", "roughly", "ca", "c", "nearly",
        "etwa", "ungefahr", "ungefaehr", "rund", "zirka", "ongeveer", "omstreeks", "rond",
        "environ", "vers", "aproximadamente",
    }
)  # fmt: skip


def _fold(s: str) -> str:
    s = unicodedata.normalize("NFKC", s).casefold()
    for a, b in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss")):
        s = s.replace(a, b)
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _tokens(s: str) -> list[str]:
    return re.findall(r"[\w']+", _fold(s))


def is_approximate_phrase(text: str | None) -> bool:
    """True if the phrase opens with an approximation marker (about, around, circa, etwa, ongeveer, ...).

    Only leading markers count, so "the circus" or "an about-face" do not. Conservative."""
    if not text:
        return False
    toks = _tokens(text)
    return bool(toks) and toks[0] in _APPROX_WORDS and len(toks) > 1


# ---------------------------------------------------------------------------------------------------------
# anchor
# ---------------------------------------------------------------------------------------------------------


def _describe(c: TemporalCandidate) -> str | None:
    """English canonical text, used only when the model gave none (to keep an unanchored phrase)."""
    if c.kind != "relative" or c.rel is None or c.unit is None:
        return None
    if c.rel in ("ago", "ahead"):
        n = c.n or 1
        u = c.unit + ("s" if n != 1 else "")
        return f"{n} {u} ago" if c.rel == "ago" else f"in {n} {u}"
    names = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
    unit = names[c.weekday or 0] if c.unit == "weekday" else c.unit
    return f"{c.rel} {unit}"


def _unresolved(c: TemporalCandidate) -> TemporalRef:
    return TemporalRef(text=c.text if c.text is not None else _describe(c))


def _mk(
    at: datetime,
    precision: str,
    *,
    text: str | None,
    approximate: bool,
    basis: Literal["explicit", "source_time"],
    anchor_dt: datetime | None = None,
) -> TemporalRef:
    return TemporalRef(
        text=text,
        at=at,
        precision=precision,  # type: ignore[arg-type]
        approximate=approximate,
        basis=basis,
        anchor=anchor_dt,
    )


def anchor(
    candidate: TemporalCandidate,
    *,
    source_time: datetime | None,
    tz: tzinfo | str,
    hemisphere: Hemisphere = "north",
    known: Mapping[str, TemporalRef] | None = None,
) -> TemporalRef:
    """Resolve a candidate into a :class:`TemporalRef` with honest basis and precision (S6-2).

    * absolute -> ``basis="explicit"``, precision as given (date = day; datetime = instant/minute/hour).
    * partial (year, year-month, season, decade) -> ``basis="explicit"`` at the *start of that period* with the
      coarse precision; never day precision.
    * relative -> ``basis="source_time"`` with the phrase's own precision ("last week" = week, "3 months ago"
      = month, "yesterday" = day, "this weekend" = day + approximate); needs ``source_time``. Without it the
      phrase is kept unanchored: ``basis="none"``, no date, phrase in ``text``.
    * duration -> a duration ref (``basis="none"``, no date); its ``Duration.anchor`` is set when an anchor
      candidate resolves. Use :func:`candidate_event_interval` for the interval.
    * relation -> ``basis="relation"`` without a date, or resolved via :func:`resolve_relation` if ``known``
      maps the target assertion id to an anchored ref.
    * none -> empty ref (``basis="none"``).

    An ``approximate`` flag on the candidate, or a leading marker in its text (about/around/circa/etwa/ongeveer
    ...), sets ``approximate`` on dated results. The result never carries a finer precision than the input.
    """
    zone = _zone(tz)
    approx = candidate.approximate or is_approximate_phrase(candidate.text)
    src = _aware(source_time, zone) if source_time is not None else None
    k = candidate.kind

    if k == "none":
        return TemporalRef(text=candidate.text)
    if k == "absolute":
        return _anchor_absolute(candidate, zone, approx)
    if k == "partial":
        return _anchor_partial(candidate, zone, approx, hemisphere)
    if k == "relative":
        if src is None:
            return _unresolved(candidate)
        out = _anchor_relative(candidate, src, approx, hemisphere)
        return out if out is not None else _unresolved(candidate)
    if k == "duration":
        assert candidate.duration_value is not None and candidate.duration_unit is not None
        anc_dt = None
        if candidate.anchor_candidate is not None:
            sub = anchor(
                candidate.anchor_candidate,
                source_time=source_time,
                tz=zone,
                hemisphere=hemisphere,
                known=known,
            )
            anc_dt = sub.at
        return TemporalRef(
            text=candidate.text,
            duration=Duration(
                value=candidate.duration_value, unit=candidate.duration_unit, anchor=anc_dt
            ),
        )
    # relation
    assert candidate.relation is not None
    if known is not None:
        target = known.get(candidate.relation.target_assertion_id)
        if target is not None:
            res = resolve_relation(candidate.relation, target)
            if res is not None:
                return res.model_copy(update={"text": candidate.text})
    return TemporalRef(text=candidate.text, basis="relation", relation=candidate.relation)


def _anchor_absolute(c: TemporalCandidate, zone: tzinfo, approx: bool) -> TemporalRef:
    if c.date_value is not None:
        return _mk(
            _midnight(c.date_value, zone), "day", text=c.text, approximate=approx, basis="explicit"
        )
    dt = c.datetime_value
    assert dt is not None
    dt = _aware(dt, zone) if dt.tzinfo is None else dt
    p = c.time_precision or ("instant" if (dt.second or dt.microsecond) else "minute")
    return _mk(_floor(dt, p), p, text=c.text, approximate=approx, basis="explicit")


def _anchor_partial(
    c: TemporalCandidate, zone: tzinfo, approx: bool, hemi: Hemisphere
) -> TemporalRef:
    assert c.year is not None
    if c.partial == "year":
        at, p = date(c.year, 1, 1), "year"
    elif c.partial == "year_month":
        assert c.month is not None
        at, p = date(c.year, c.month, 1), "month"
    elif c.partial == "season":
        assert c.season is not None
        at, p = date(c.year, season_start_month(c.season, hemi), 1), "season"
    else:
        at, p = date(c.year, 1, 1), "decade"
    return _mk(_midnight(at, zone), p, text=c.text, approximate=approx, basis="explicit")


def _anchor_relative(
    c: TemporalCandidate, src: datetime, approx: bool, hemi: Hemisphere
) -> TemporalRef | None:
    assert c.rel is not None and c.unit is not None
    try:
        at, p, extra_approx = _relative_target(c, src)
    except (OverflowError, ValueError):
        return None
    return _mk(
        at,
        p,
        text=c.text if c.text is not None else _describe(c),
        approximate=approx or extra_approx,
        basis="source_time",
        anchor_dt=src,
    )


def _relative_target(c: TemporalCandidate, src: datetime) -> tuple[datetime, str, bool]:
    rel, unit = c.rel, c.unit
    assert rel is not None and unit is not None
    if rel in ("ago", "ahead"):
        n = (c.n or 1) * (-1 if rel == "ago" else 1)
        if unit in ("minute", "hour"):
            return _floor(_abs_add(src, timedelta(**{unit + "s": n})), unit), unit, False
        if unit == "day":
            return _shift(_floor(src, "day"), "day", n), "day", False
        if unit == "week":
            return _shift(_floor(src, "week"), "week", n), "week", False
        if unit == "month":
            return _shift(_floor(src, "month"), "month", n), "month", False
        if unit == "year":
            return _shift(_floor(src, "year"), "year", n), "year", False
        raise ValueError(unit)
    step = {"last": -1, "this": 0, "next": 1}[rel]
    if unit in ("day", "week", "month", "season", "year"):
        return _shift(_floor(src, unit), unit, step), unit, False
    d = src.date()
    if unit == "weekday":
        assert c.weekday is not None
        if rel == "this":
            target = d - timedelta(days=d.weekday()) + timedelta(days=c.weekday)
        elif rel == "last":
            target = d - timedelta(days=(d.weekday() - c.weekday) % 7 or 7)
        else:
            target = d + timedelta(days=(c.weekday - d.weekday()) % 7 or 7)
        return _midnight(target, src.tzinfo), "day", False
    # weekend: the Saturday starts it; precision day + approximate (it spans two days)
    sat = (
        d + timedelta(days=(5 - d.weekday()) % 7)
        if d.weekday() < 5
        else d - timedelta(days=d.weekday() - 5)
    )
    if rel == "last":
        sat = sat - timedelta(days=7)
    elif rel == "next":
        if d.weekday() < 5:
            raise ValueError("'next weekend' from a weekday is ambiguous")  # kept unanchored
        sat = sat + timedelta(days=7)
    return _midnight(sat, src.tzinfo), "day", True


def candidate_event_interval(
    candidate: TemporalCandidate,
    *,
    source_time: datetime | None,
    tz: tzinfo | str,
    hemisphere: Hemisphere = "north",
    known: Mapping[str, TemporalRef] | None = None,
) -> Interval | None:
    """Event-time interval for a candidate, or ``None`` (event time left empty).

    Handles the anchored-duration case ("for three years until 2024") that :func:`anchor` can only keep as a
    duration; everything else is ``event_interval(anchor(...))``.
    """
    zone = _zone(tz)
    if candidate.kind == "duration":
        if candidate.anchor_candidate is None or candidate.duration_value is None:
            return None
        sub = anchor(
            candidate.anchor_candidate,
            source_time=source_time,
            tz=zone,
            hemisphere=hemisphere,
            known=known,
        )
        assert candidate.duration_unit is not None
        dur = Duration(value=candidate.duration_value, unit=candidate.duration_unit)
        return duration_interval(dur, sub, role=candidate.anchor_role)
    return event_interval(
        anchor(candidate, source_time=source_time, tz=zone, hemisphere=hemisphere, known=known)
    )


# ---------------------------------------------------------------------------------------------------------
# as-of helpers (S6-3)
# ---------------------------------------------------------------------------------------------------------


def true_at(axes: TimeAxes, t: datetime | TemporalRef | Interval) -> bool | None:
    """Answer "What was true on ``t``": uses *event time*. An instant is inside the event interval or not; a period
    (ref or interval) counts if it overlaps. ``None`` when the assertion has no (resolved) event time or the
    comparison is undecidable. Source and recorded time are not consulted."""
    if axes.event is None:
        return None
    return overlaps(axes.event, t)


def known_at(
    axes: TimeAxes,
    t: datetime,
    *,
    mode: Literal["both", "source", "recorded"] = "both",
) -> bool | None:
    """Answer "What did we know on ``t``": uses *source time* (when said) and *recorded time* (when stored), never
    event time. ``mode="both"`` (default): known once every available stamp is ``<= t`` (we cannot know
    something before it was said, nor before it was stored). ``"source"`` / ``"recorded"`` use one stamp only
    (replay of historical logs, where recorded time is the import date). ``None`` if the needed stamp is
    missing."""
    if t.tzinfo is None:
        raise ValueError("t must be timezone-aware")
    if mode == "source":
        stamps = [axes.source_time]
    elif mode == "recorded":
        stamps = [axes.recorded_at]
    else:
        stamps = [axes.source_time, axes.recorded_at]
    present = [s for s in stamps if s is not None]
    if not present:
        return None
    if mode != "both" and len(present) < len(stamps):
        return None
    return all(s <= t for s in present)


# ---------------------------------------------------------------------------------------------------------
# optional multilingual relative-phrase normaliser (English, German, Dutch); None when unsure
# ---------------------------------------------------------------------------------------------------------

_FIXED: dict[tuple[str, ...], tuple[str, str, int | None]] = {
    ("yesterday",): ("ago", "day", 1),
    ("today",): ("this", "day", None),
    ("tomorrow",): ("ahead", "day", 1),
    ("the", "day", "before", "yesterday"): ("ago", "day", 2),
    ("the", "day", "after", "tomorrow"): ("ahead", "day", 2),
    ("gestern",): ("ago", "day", 1),
    ("heute",): ("this", "day", None),
    ("morgen",): ("ahead", "day", 1),
    ("vorgestern",): ("ago", "day", 2),
    ("uebermorgen",): ("ahead", "day", 2),
    ("gisteren",): ("ago", "day", 1),
    ("vandaag",): ("this", "day", None),
    ("eergisteren",): ("ago", "day", 2),
    ("overmorgen",): ("ahead", "day", 2),
}
_LAST_W = frozenset({"last", "previous", "letzte", "letzten", "letztes", "letzter", "vergangene",
                     "vergangenen", "vergangenes", "vorige", "vorig", "vorigen"})  # fmt: skip
_NEXT_W = frozenset({"next", "naechste", "naechsten", "naechstes", "naechster", "kommende",
                     "kommenden", "kommendes", "volgende", "volgend", "komende", "komend"})  # fmt: skip
_THIS_W = frozenset({"this", "current", "diese", "diesen", "dieses", "dieser", "deze", "dit"})
_SPAN_UNITS: dict[str, str] = {
    "day": "day", "week": "week", "month": "month", "year": "year", "weekend": "weekend",
    "tag": "day", "woche": "week", "monat": "month", "jahr": "year", "wochenende": "weekend",
    "dag": "day", "maand": "month", "jaar": "year",
}  # fmt: skip
_WEEKDAYS: dict[str, int] = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6,
    "montag": 0, "dienstag": 1, "mittwoch": 2, "donnerstag": 3, "freitag": 4, "samstag": 5,
    "sonnabend": 5, "sonntag": 6,
    "maandag": 0, "dinsdag": 1, "woensdag": 2, "donderdag": 3, "vrijdag": 4, "zaterdag": 5,
    "zondag": 6,
}  # fmt: skip
_EN_UNITS: dict[str, str] = {
    "minute": "minute", "minutes": "minute", "hour": "hour", "hours": "hour", "day": "day",
    "days": "day", "week": "week", "weeks": "week", "month": "month", "months": "month",
    "year": "year", "years": "year",
}  # fmt: skip
_DE_UNITS: dict[str, str] = {
    "minute": "minute", "minuten": "minute", "stunde": "hour", "stunden": "hour", "tag": "day",
    "tage": "day", "tagen": "day", "woche": "week", "wochen": "week", "monat": "month",
    "monate": "month", "monaten": "month", "jahr": "year", "jahre": "year", "jahren": "year",
}  # fmt: skip
_NL_UNITS: dict[str, str] = {
    "minuut": "minute", "minuten": "minute", "uur": "hour", "uren": "hour", "dag": "day",
    "dagen": "day", "week": "week", "weken": "week", "maand": "month", "maanden": "month",
    "jaar": "year", "jaren": "year",
}  # fmt: skip
_NUMBERS: dict[str, int] = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
    "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "ein": 1, "eine": 1, "einer": 1, "einem": 1, "einen": 1, "zwei": 2, "drei": 3, "vier": 4,
    "fuenf": 5, "sechs": 6, "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwoelf": 12,
    "een": 1, "twee": 2, "drie": 3, "vijf": 5, "zes": 6, "zeven": 7, "negen": 9, "tien": 10, "twaalf": 12,
}  # fmt: skip
_ALL_UNITS = {**_EN_UNITS, **_DE_UNITS, **_NL_UNITS}
_NL_ONLY = frozenset(set(_NL_UNITS) - set(_EN_UNITS) - set(_DE_UNITS))


def _number(tok: str) -> int | None:
    if tok.isascii() and tok.isdigit():
        n = int(tok)
        return n if 1 <= n <= 100_000 else None
    return _NUMBERS.get(tok)


def _cand(
    rel: str, unit: str, n: int | None, weekday: int | None, text: str, approx: bool
) -> TemporalCandidate:
    return TemporalCandidate(
        kind="relative",
        text=text,
        approximate=approx,
        rel=rel,  # type: ignore[arg-type]
        unit=unit,  # type: ignore[arg-type]
        n=n,
        weekday=weekday,
    )


def normalise_relative_phrase(phrase: str) -> TemporalCandidate | None:
    """Map a short English/German/Dutch relative phrase to a candidate, or ``None`` if not sure.

    Covers yesterday/today/tomorrow (and the day before/after), ``last|next|this`` + week/month/year/day/
    weekend/weekday name, and ``N <unit> ago`` / ``in N <unit>`` forms (German ``vor N``, Dutch ``N ... geleden``
    / ``over N``). A leading approximation marker sets ``approximate``. Ambiguous wordings (English "past
    week", Dutch "afgelopen week", "over 3 weeks" which is English-ambiguous) return ``None``: never guess.
    """
    if re.search(r"[-+\u2212\u2013]\s*\d", phrase):
        return None  # a signed number is not a phrase we understand
    toks = _tokens(phrase)
    approx = False
    while len(toks) > 1 and toks[0] in _APPROX_WORDS:
        approx, toks = True, toks[1:]
    if not toks:
        return None
    text = phrase.strip()
    if tuple(toks) in _FIXED:
        rel, unit, n = _FIXED[tuple(toks)]
        return _cand(rel, unit, n, None, text, approx)
    if len(toks) == 2:
        det, word = toks
        rel = (
            "last"
            if det in _LAST_W
            else "next"
            if det in _NEXT_W
            else "this"
            if det in _THIS_W
            else None
        )
        if rel is None:
            return None
        if word in _SPAN_UNITS:
            return _cand(rel, _SPAN_UNITS[word], None, None, text, approx)
        if word in _WEEKDAYS:
            return _cand(rel, "weekday", None, _WEEKDAYS[word], text, approx)
        return None
    if len(toks) == 3:
        a, b, c = toks
        # "<N> <unit> ago" (en) / "<N> <unit> geleden" (nl)
        if c == "ago" and a != "in" and b in _EN_UNITS and (n := _number(a)) is not None:
            return _cand("ago", _EN_UNITS[b], n, None, text, approx)
        if c == "geleden" and b in _NL_UNITS and (n := _number(a)) is not None:
            return _cand("ago", _NL_UNITS[b], n, None, text, approx)
        n = _number(b)
        if n is None:
            return None
        if a == "in" and c in _ALL_UNITS:
            return _cand("ahead", _ALL_UNITS[c], n, None, text, approx)
        if a == "vor" and c in _DE_UNITS:
            return _cand("ago", _DE_UNITS[c], n, None, text, approx)
        if a == "over" and c in _NL_ONLY:
            return _cand("ahead", _NL_UNITS[c], n, None, text, approx)
    return None
