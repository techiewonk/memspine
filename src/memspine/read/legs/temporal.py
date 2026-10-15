"""R3-04 (R-07): the temporal leg over event intervals.

The question names a :class:`TimeWindow` ``[start, end)``; relative phrases ("last week") resolve
against the question's reference time (the caller's as-of, else now), never against the stored
data. A record matches when an *event interval* of it overlaps the window:

* ``[valid_from, valid_to)`` when the record has a closed validity (a fact that held for a while);
* the instant ``valid_from`` otherwise (a raw turn, or a fact still open: utterance time, R-07's
  option B fallback);
* the ``happened:`` date span of a mined fact when the record carries one (the event-time field
  of the envelope; a fact said days after it happened is found by its own date).

Matches rank by closeness of the best interval to the window middle (ties: chronological).
Score is ``1 / (1 + days)`` from that middle, so it lies in ``(0, 1]``.

FU-TEMPORAL: the old ``read.temporal_*`` rules are honoured here (see ``LegContext``): the
``overlap`` rank, ``soft`` fill, turns that name a date (``mentions``), the ``happened:`` date
(``event_dates``, with the old "start of the overlap" distance), relative phrases (only with a
``reference_time``) and yearless dates (only with a ``year_ref``). Utterance instants and mention /
happened dates rank exactly as ``models.encoders.query.temporal_query.temporal_leg`` did; closed
validity intervals and the precision weight are the v2 additions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from memspine.config.constants import TEMPORAL_SOFT_MARGIN_DAYS
from memspine.core.event_date import happened_of, label_span
from memspine.models.encoders.query.temporal_query import mentioned_spans, query_interval
from memspine.read.legs.core import Hit, LegContext, LegOutput
from memspine.store.records import MemoryRecord, chrono_key

__all__ = ["TemporalLeg", "TimeWindow", "event_intervals"]

_Interval = tuple[datetime, datetime]


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


@dataclass(frozen=True)
class TimeWindow:
    """A half-open window ``[start, end)`` in UTC."""

    start: datetime
    end: datetime

    @classmethod
    def from_query(
        cls,
        query: str,
        reference_time: datetime | None,
        *,
        year_ref: datetime | None = None,
        week: str = "calendar",
    ) -> TimeWindow | None:
        """The first span the question names, or None. ``reference_time`` anchors relative
        phrases; without it only absolute dates resolve."""
        span = query_interval(query, reference_time, week=week, year_ref=year_ref)  # type: ignore[arg-type]  # CI-GREEN: pre-existing typing debt
        return cls(*span) if span else None

    @property
    def middle(self) -> datetime:
        return self.start + (self.end - self.start) / 2

    def overlaps(self, lo: datetime, hi: datetime) -> bool:
        """An interval ``[lo, hi)`` overlaps; an instant (``hi <= lo``) is inside if
        ``start <= lo < end``."""
        if hi <= lo:
            return self.start <= lo < self.end
        return lo < self.end and hi > self.start

    def distance_days(self, lo: datetime, hi: datetime) -> float:
        """Days from the window middle to the nearest point of ``[lo, hi]``."""
        mid = self.middle
        if lo <= mid <= max(hi, lo):
            return 0.0
        return min(abs((mid - lo).total_seconds()), abs((mid - hi).total_seconds())) / 86400.0


def _happened_interval(record: MemoryRecord) -> _Interval | None:
    label = happened_of(record)
    days = label_span(label) if label else None
    if days is None:
        return None
    first = datetime(days[0].year, days[0].month, days[0].day, tzinfo=UTC)
    after = datetime(days[1].year, days[1].month, days[1].day, tzinfo=UTC) + timedelta(days=1)
    return first, after


def event_intervals(record: MemoryRecord) -> list[_Interval]:
    """The event intervals of a record (see the module docstring); instants are ``(t, t)``."""
    lo = _aware(record.valid_from)
    out: list[_Interval] = []
    if record.valid_to is not None and _aware(record.valid_to) > lo:
        out.append((lo, _aware(record.valid_to)))
    else:
        out.append((lo, lo))
    happened = _happened_interval(record)
    if happened is not None:
        out.append(happened)
    return out


def precision_weight(window: TimeWindow, interval: _Interval) -> float:
    """W2-14: the share of a stated period that the window covers, in ``(0, 1]``. A day-precision
    period inside a day window weighs 1; a year-precision period weighs about 1/365 for it."""
    span = (interval[1] - interval[0]).total_seconds()
    if span <= 0:
        return 1.0
    covered = (min(window.end, interval[1]) - max(window.start, interval[0])).total_seconds()
    return covered / span if covered > 0 else 1.0


class TemporalLeg:
    """The temporal leg: records whose event interval overlaps the question's window."""

    name = "temporal"
    family = "structured"

    def __init__(self, layers: tuple[str, ...] = ("L0", "L1"), name: str = "temporal") -> None:
        self.layers = layers
        self.name = name

    async def run(self, ctx: LegContext) -> LegOutput:
        records = ctx.records_for(self.layers)
        if not records:
            return LegOutput([], f"no_records:{'+'.join(self.layers)}")
        window = TimeWindow.from_query(
            ctx.query, ctx.reference_time, year_ref=ctx.year_ref, week=ctx.week
        )
        if window is None:
            return LegOutput([], "no_window")
        asked = _content_words(ctx.query) if ctx.temporal_rank == "overlap" else frozenset()
        scored: list[tuple[int, float, MemoryRecord]] = []  # (-shared words, days, record)
        weights: dict[str, float] = {}
        for r in records:
            days = self._match(r, window, ctx, weights)
            if days is not None:
                shared = len(asked & _content_words(r.content)) if asked else 0
                scored.append((-shared, days, r))
        if ctx.precision_aware:

            def rank(it: tuple[int, float, MemoryRecord]) -> tuple[int, float, Any]:
                w = weights.get(it[2].record_id, 1.0)
                return (it[0], -w / (1.0 + it[1]), chrono_key(it[2]))

            scored.sort(key=rank)
        else:
            scored.sort(key=lambda item: (item[0], item[1], chrono_key(item[2])))
        hits: list[Hit] = []
        ceiling = 1.0
        for _, d, r in scored[: ctx.fetch_k]:
            # scores never rise down the list, whatever the rank rule put first
            ceiling = min(ceiling, weights.get(r.record_id, 1.0) / (1.0 + d))
            hits.append(Hit(r.record_id, ceiling))
        if ctx.temporal_soft and len(hits) < ctx.fetch_k:
            inside = {h.record_id for h in hits}
            for gap, r in _nearest_outside(records, window, inside, ctx.fetch_k - len(hits)):
                ceiling = min(ceiling, 0.5 / (1.0 + gap))
                hits.append(Hit(r.record_id, ceiling))
        return LegOutput(hits, None if hits else "no_overlap")

    @staticmethod
    def _match(
        r: MemoryRecord, window: TimeWindow, ctx: LegContext, weights: dict[str, float]
    ) -> float | None:
        """Days from the window middle of the record's best matching time, or None."""
        best: float | None = None

        def take(d: float) -> None:
            nonlocal best
            best = d if best is None else min(best, d)

        def from_middle(t: datetime) -> float:
            return abs((window.middle - t).total_seconds()) / 86400.0

        happened = (
            _happened_interval(r) if (ctx.temporal_event_dates or ctx.precision_aware) else None
        )
        lo = _aware(r.valid_from)
        if r.valid_to is not None and _aware(r.valid_to) > lo:
            # a state that held for a while: matches while it overlaps the window
            hi = _aware(r.valid_to)
            if window.overlaps(lo, hi):
                take(window.distance_days(lo, hi))
        elif not (happened is not None and ctx.precision_aware):
            # the utterance instant (precision-aware: it is the moved start of a happened period)
            if window.start <= lo < window.end:
                take(from_middle(lo))
        if happened is not None and window.overlaps(*happened):
            if ctx.precision_aware:
                take(window.distance_days(*happened))
                weights[r.record_id] = precision_weight(window, happened)
            else:  # the old rule: the start of the overlap, so a long period is not "at" the middle
                take(from_middle(max(happened[0], window.start)))
        if ctx.temporal_mentions:
            for m_lo, m_hi in mentioned_spans(r):
                if max(m_lo, window.start) < min(m_hi, window.end):
                    take(from_middle(max(m_lo, window.start)))
        return best


def _nearest_outside(
    records: Sequence[MemoryRecord], window: TimeWindow, inside: set[str], n: int
) -> list[tuple[float, MemoryRecord]]:
    """N44 (``read.temporal_soft``): the records nearest outside the window, within one window
    length (at least ``TEMPORAL_SOFT_MARGIN_DAYS``), by days of gap, ties chronological."""
    margin = max(window.end - window.start, timedelta(days=TEMPORAL_SOFT_MARGIN_DAYS))
    near: list[tuple[float, MemoryRecord]] = []
    for r in records:
        said = _aware(r.valid_from)
        if r.record_id in inside or not (window.start - margin <= said < window.end + margin):
            continue
        gap = (window.start - said) if said < window.start else (said - window.end)
        near.append((gap.total_seconds() / 86400.0, r))
    near.sort(key=lambda pair: (pair[0], chrono_key(pair[1])))
    return near[:n]


def _content_words(text: str) -> frozenset[str]:
    from memspine.models.encoders.query.shape import content_words

    return content_words(text)
