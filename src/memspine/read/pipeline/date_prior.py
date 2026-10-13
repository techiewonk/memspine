"""R14-M-7 (``read.date_prior``): a bounded session-date prior at fusion.

A question that names an explicit date ("on 7 May 2023", "in March 2024", an ISO date) points at
the session of that date, yet fusion treats the date as ordinary words. This module finds the
question's date span with the existing temporal parser (:func:`query_interval`; never the system
clock: relative phrases resolve only against an explicit as-of) and multiplies the fused score of
candidates whose event or session date lies within ``read.date_prior_days`` of the span by a
bounded factor (at most ``1 + read.date_prior_boost``, linear in closeness). Only ids already
fused by some leg are touched, so the prior re-orders and protects; it never adds an item.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta

from memspine.core.event_date import happened_of, label_span
from memspine.models.encoders.query.temporal_query import query_interval
from memspine.store.records import MemoryRecord

#: a span longer than this (a bare year, "in 2023") names no session, so it earns no prior.
MAX_SPAN_DAYS = 31
#: at most this many boosted candidates are protected into the rerank pool.
PROTECT_MAX = 5
#: a boosted candidate is protected only when some leg ranks it within this depth.
PROTECT_LEG_DEPTH = 10


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def prior_span(
    query: str, *, anchor: datetime | None = None, year_ref: datetime | None = None
) -> tuple[datetime, datetime] | None:
    """The question's short date span, or None (no date, or a span too long to name a session)."""
    span = query_interval(query, anchor, year_ref=year_ref)
    if span is None or span[1] - span[0] > timedelta(days=MAX_SPAN_DAYS):
        return None
    return span


def _gap_days(record: MemoryRecord, span: tuple[datetime, datetime]) -> float:
    """Days between the record's nearest date (session or ``happened:``) and the span."""
    start, end = span
    times = [_aware(record.valid_from)]
    label = happened_of(record)
    days = label_span(label) if label else None
    if days is not None:
        times.append(datetime(days[0].year, days[0].month, days[0].day, tzinfo=UTC))
    best = float("inf")
    for t in times:
        gap = (start - t) if t < start else (t - end if t >= end else timedelta(0))
        best = min(best, gap.total_seconds() / 86400.0)
    return best


def apply_prior(
    fused: Sequence[tuple[str, float]],
    records: Mapping[str, MemoryRecord],
    span: tuple[datetime, datetime],
    *,
    window_days: int,
    boost: float,
) -> tuple[list[tuple[str, float]], dict[str, float]]:
    """``fused`` re-scored and re-sorted (stable), plus the applied multiplier per boosted id.

    The multiplier is ``1 + boost * (1 - gap / window_days)`` for a gap inside the window, so it
    never exceeds ``1 + boost`` and is exactly 1 at and beyond the window edge."""
    if window_days < 1 or boost <= 0.0:
        return list(fused), {}
    applied: dict[str, float] = {}
    out: list[tuple[str, float]] = []
    for rid, score in fused:
        rec = records.get(rid)
        gap = _gap_days(rec, span) if rec is not None else float("inf")
        if gap < window_days:
            mult = 1.0 + boost * (1.0 - gap / window_days)
            applied[rid] = mult
            out.append((rid, score * mult))
        else:
            out.append((rid, score))
    out.sort(key=lambda pair: pair[1], reverse=True)  # stable: ties keep fused order
    return out, applied


def protect_ids(
    boosted: Mapping[str, float], leg_rank: Mapping[str, tuple[str, int]]
) -> dict[str, str]:
    """Boosted ids that some leg ranks within the depth, strongest boost first, capped."""
    eligible = [
        rid for rid in boosted if rid in leg_rank and leg_rank[rid][1] <= PROTECT_LEG_DEPTH
    ]
    eligible.sort(key=lambda rid: (-boosted[rid], leg_rank[rid][1]))
    return {rid: "date_prior" for rid in eligible[:PROTECT_MAX]}
