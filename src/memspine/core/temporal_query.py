"""Query-side temporal and metadata legs for fused retrieval (C3').

Deterministic, no model on the read path. :func:`query_interval` pulls one
ABSOLUTE date span out of a query ("2023-05-07", "7 May 2023", "May 7, 2023",
"May 2023", "in 2023"); relative phrases ("last week") are deliberately not
resolved, because a read has no trustworthy anchor time of its own.
:func:`temporal_leg` ranks live records whose event time (``valid_from``) falls
in that span by closeness to its midpoint (B2: optionally also a mined fact whose
``happened:`` date overlaps it); :func:`metadata_leg` ranks records
whose ``entity`` is named in the query, newest first. Both return
``record_id``-bearing hits that :func:`~memspine.services.lexical.base.rrf_fuse`
fuses as extra legs.
"""

from __future__ import annotations

import calendar
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from memspine.core.event_date import happened_of, label_span
from memspine.core.records import MemoryRecord, chrono_key

__all__ = ["LegHit", "metadata_leg", "query_interval", "temporal_leg"]

_MONTHS = {name.lower(): i for i, name in enumerate(calendar.month_name) if name} | {
    name.lower(): i for i, name in enumerate(calendar.month_abbr) if name
}
_MON = r"(?P<mon>" + "|".join(sorted(_MONTHS, key=len, reverse=True)) + r")\.?"
_ISO = re.compile(r"\b(?P<y>(?:19|20)\d{2})-(?P<m>\d{1,2})-(?P<d>\d{1,2})\b")
_DAY_MON_YEAR = re.compile(
    rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+{_MON}\s*,?\s+(?P<y>(?:19|20)\d{{2}})\b", re.I
)
_MON_DAY_YEAR = re.compile(
    rf"\b{_MON}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s*,?\s+(?P<y>(?:19|20)\d{{2}})\b", re.I
)
_MON_YEAR = re.compile(rf"\b{_MON}\s*,?\s+(?P<y>(?:19|20)\d{{2}})\b", re.I)
_YEAR = re.compile(r"\b(?:in|during|of|since|by)\s+(?P<y>(?:19|20)\d{2})\b", re.I)


@dataclass(frozen=True, slots=True)
class LegHit:
    record_id: str
    score: float


def _day(y: int, m: int, d: int) -> tuple[datetime, datetime] | None:
    try:
        start = datetime(y, m, d, tzinfo=UTC)
    except ValueError:
        return None
    return start, start + timedelta(days=1)


def query_interval(query: str) -> tuple[datetime, datetime] | None:
    """The first absolute ``[start, end)`` span named in ``query``, or None."""
    if m := _ISO.search(query):
        return _day(int(m["y"]), int(m["m"]), int(m["d"]))
    for pattern in (_DAY_MON_YEAR, _MON_DAY_YEAR):
        if m := pattern.search(query):
            return _day(int(m["y"]), _MONTHS[m["mon"].lower()], int(m["d"]))
    if m := _MON_YEAR.search(query):
        y, mon = int(m["y"]), _MONTHS[m["mon"].lower()]
        start = datetime(y, mon, 1, tzinfo=UTC)
        end = datetime(y + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
        return start, end
    if m := _YEAR.search(query):
        y = int(m["y"])
        return datetime(y, 1, 1, tzinfo=UTC), datetime(y + 1, 1, 1, tzinfo=UTC)
    return None


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def temporal_leg(
    query: str, records: Iterable[MemoryRecord], top_k: int, *, event_dates: bool = False
) -> list[LegHit]:
    """Records whose event time lies in the query's span, closest to its middle first.

    ``event_dates`` (B2, ``read.temporal_leg_event_dates``): a record whose
    ``happened:`` date (a mined fact's event date) overlaps the span also enters, at
    the start of the overlap, so an event said days after it happened is found by its
    own date. A record matching both ways keeps the closer time. Ties: ``chrono_key``.
    """
    span = query_interval(query)
    if span is None:
        return []
    start, end = span
    mid = start + (end - start) / 2
    scored: list[tuple[float, MemoryRecord]] = []
    for r in records:
        times = []
        said = _aware(r.valid_from)
        if start <= said < end:
            times.append(said)
        if event_dates and (at := _happened_in(r, start, end)) is not None:
            times.append(at)
        if times:
            scored.append((min(abs((t - mid).total_seconds()) for t in times), r))
    scored.sort(key=lambda pair: (pair[0], chrono_key(pair[1])))
    return [LegHit(r.record_id, 1.0) for _, r in scored[:top_k]]


def _happened_in(record: MemoryRecord, start: datetime, end: datetime) -> datetime | None:
    """The start of the overlap of ``record``'s happened span with ``[start, end)``."""
    label = happened_of(record)
    days = label_span(label) if label else None
    if days is None:
        return None
    first = datetime(days[0].year, days[0].month, days[0].day, tzinfo=UTC)
    after = datetime(days[1].year, days[1].month, days[1].day, tzinfo=UTC) + timedelta(days=1)
    lo, hi = max(first, start), min(after, end)
    return lo if lo < hi else None


def metadata_leg(query: str, records: Iterable[MemoryRecord], top_k: int) -> list[LegHit]:
    """Records whose ``entity`` is named (whole word, case-insensitive) in the query."""
    text = query.lower()
    named = [
        r
        for r in records
        if r.entity and re.search(rf"(?<!\w){re.escape(r.entity.lower())}(?!\w)", text)
    ]
    named.sort(key=lambda r: (-_aware(r.valid_from).timestamp(), chrono_key(r)))
    return [LegHit(r.record_id, 1.0) for r in named[:top_k]]
