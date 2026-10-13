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
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from memspine.core.event_date import happened_of, label_span
from memspine.models.encoders.query.temporal_query import query_interval
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
        span = query_interval(query, reference_time, week=week, year_ref=year_ref)
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


def event_intervals(record: MemoryRecord) -> list[_Interval]:
    """The event intervals of a record (see the module docstring); instants are ``(t, t)``."""
    lo = _aware(record.valid_from)
    out: list[_Interval] = []
    if record.valid_to is not None and _aware(record.valid_to) > lo:
        out.append((lo, _aware(record.valid_to)))
    else:
        out.append((lo, lo))
    label = happened_of(record)
    days = label_span(label) if label else None
    if days is not None:
        first = datetime(days[0].year, days[0].month, days[0].day, tzinfo=UTC)
        after = datetime(days[1].year, days[1].month, days[1].day, tzinfo=UTC) + timedelta(days=1)
        out.append((first, after))
    return out


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
        scored: list[tuple[float, MemoryRecord]] = []
        for r in records:
            best: float | None = None
            for lo, hi in event_intervals(r):
                if window.overlaps(lo, hi):
                    d = window.distance_days(lo, hi)
                    best = d if best is None else min(best, d)
            if best is not None:
                scored.append((best, r))
        scored.sort(key=lambda item: (item[0], chrono_key(item[1])))
        hits = [Hit(r.record_id, 1.0 / (1.0 + d)) for d, r in scored[: ctx.fetch_k]]
        return LegOutput(hits, None if hits else "no_overlap")
