"""#36 / #37: structured filters and legs on the read path. Pure functions, no model.

**#37, search date filters.** :class:`DateFilter` holds bounds on a record's three
time columns: ``valid_from`` (when the fact holds from, the event time), ``valid_to``
(when it stopped holding; None = still valid) and ``recorded_at`` (when memspine
stored it). Each ``*_after`` bound is inclusive and each ``*_before`` bound exclusive,
so ``valid_from_after=2023-05-01, valid_from_before=2023-06-01`` is exactly May 2023.
An open ``valid_to`` counts as later than any date: it passes every ``valid_to_after``
bound and fails every ``valid_to_before`` bound. ``mode="and"`` (default) keeps a
record that meets every bound given; ``mode="or"`` one that meets any of them.

**#36, the persons / time-expression leg.** The LLM planner (``plan@v3``) names the
people a question is about and copies its time expression ("in May 2023", "last
week"). :func:`time_expr_span` turns the expression into a ``[start, end)`` span by the
same deterministic rules the read path already uses: an absolute date, month or year
(:func:`~memspine.core.temporal_query.query_interval`), else a relative phrase resolved
by the H1 resolver (:func:`~memspine.core.temporal_resolve.resolve`) against an anchor.
:func:`person_time_leg` ranks the records that fall in the span and/or match a person
(a ``person:`` tag when the record has any, else its ``entity``); records matching both
come first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal

from memspine.core.records import MemoryRecord
from memspine.core.temporal_query import LegHit, query_interval
from memspine.core.temporal_resolve import WeekMode, resolve

__all__ = [
    "PERSON_TAG_PREFIX",
    "DateBound",
    "DateFilter",
    "active_date_filter",
    "date_filter_scope",
    "person_matches",
    "person_time_leg",
    "time_expr_span",
    "to_utc",
]

#: The tag prefix that names a person a record is about (``person:melanie``).
PERSON_TAG_PREFIX = "person:"

#: A date bound as callers pass it: a datetime, a date (midnight UTC) or ISO text.
DateBound = datetime | date | str

FilterMode = Literal["and", "or"]


def to_utc(value: DateBound) -> datetime:
    """``value`` as an aware UTC datetime. A naive datetime is read as UTC, a date
    (or a date-only ISO string) as its midnight UTC. Raises ``ValueError`` on bad text."""
    if isinstance(value, str):
        # Python 3.11+ reads a date-only string as its midnight.
        value = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, date):
        return datetime.combine(value, time(0), tzinfo=UTC)
    raise ValueError(f"not a date bound: {value!r}")


@dataclass(frozen=True, slots=True)
class DateFilter:
    """#37: bounds on ``valid_from`` / ``valid_to`` / ``recorded_at`` (see the module
    docstring for the semantics). Build it with :meth:`build`, which returns None when
    no bound is given, so an unfiltered call never carries a filter."""

    valid_from_after: datetime | None = None
    valid_from_before: datetime | None = None
    valid_to_after: datetime | None = None
    valid_to_before: datetime | None = None
    recorded_after: datetime | None = None
    recorded_before: datetime | None = None
    mode: FilterMode = "and"

    @classmethod
    def build(
        cls,
        *,
        valid_from_after: DateBound | None = None,
        valid_from_before: DateBound | None = None,
        valid_to_after: DateBound | None = None,
        valid_to_before: DateBound | None = None,
        recorded_after: DateBound | None = None,
        recorded_before: DateBound | None = None,
        mode: str = "and",
    ) -> DateFilter | None:
        """The filter for these bounds, or None when every bound is None."""
        if mode not in ("and", "or"):
            raise ValueError(f"date_filter_mode must be 'and' or 'or', got {mode!r}")
        raw = {
            "valid_from_after": valid_from_after,
            "valid_from_before": valid_from_before,
            "valid_to_after": valid_to_after,
            "valid_to_before": valid_to_before,
            "recorded_after": recorded_after,
            "recorded_before": recorded_before,
        }
        bounds = {k: to_utc(v) for k, v in raw.items() if v is not None}
        if not bounds:
            return None
        return cls(**bounds, mode="or" if mode == "or" else "and")

    def bounds(self) -> dict[str, datetime]:
        """The bounds that are set, by name."""
        return {
            f.name: getattr(self, f.name)
            for f in fields(self)
            if f.name != "mode" and getattr(self, f.name) is not None
        }

    def matches(self, record: MemoryRecord) -> bool:
        """Does ``record`` pass the filter (every bound under ``and``, any under ``or``)?"""
        checks = [_check(name, bound, record) for name, bound in self.bounds().items()]
        return all(checks) if self.mode == "and" else any(checks)


_ACTIVE: ContextVar[DateFilter | None] = ContextVar("memspine_date_filter", default=None)


def active_date_filter() -> DateFilter | None:
    """The date filter of the ``search()`` / ``read()`` call in progress, if any."""
    return _ACTIVE.get()


@contextmanager
def date_filter_scope(date_filter: DateFilter | None) -> Iterator[None]:
    """Make ``date_filter`` the active filter for this task while the block runs.

    A None filter leaves the scope untouched, so an unfiltered call nested in a
    filtered one (or the reverse) is never affected by accident."""
    if date_filter is None:
        yield
        return
    token = _ACTIVE.set(date_filter)
    try:
        yield
    finally:
        _ACTIVE.reset(token)


def _aware(t: datetime) -> datetime:
    return t if t.tzinfo is not None else t.replace(tzinfo=UTC)


def _check(name: str, bound: datetime, record: MemoryRecord) -> bool:
    column, side = name.rsplit("_", 1)
    if column == "recorded":
        column = "recorded_at"
    value: datetime | None = getattr(record, column)
    if value is None:  # an open valid_to: later than any date
        return side == "after"
    value = _aware(value)
    return value >= bound if side == "after" else value < bound


def time_expr_span(
    expr: str | None, anchor: datetime, *, week: WeekMode = "calendar"
) -> tuple[datetime, datetime] | None:
    """#36: the ``[start, end)`` span a time expression names, or None.

    An absolute date, month or year first (``query_interval``); else the first relative
    phrase the H1 resolver finds, resolved against ``anchor`` (``week`` as in
    ``read.relative_week``). Vague phrases ("recently") resolve to nothing."""
    if expr is None or not expr.strip():
        return None
    span = query_interval(expr)
    if span is not None:
        return span
    found = resolve(expr, anchor, week=week)
    if not found:
        return None
    first, last = found[0].first, found[0].last
    start = datetime.combine(first, time(0), tzinfo=UTC)
    return start, datetime.combine(last, time(0), tzinfo=UTC) + timedelta(days=1)


def _words(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.casefold()))


def person_matches(record: MemoryRecord, persons: Sequence[str]) -> bool:
    """#36: is ``record`` about one of ``persons``?

    A record with ``person:`` tags matches on them only (a tag value equal to the
    name, or containing it as a word: ``person:melanie smith`` matches "Melanie");
    without such tags, its ``entity`` must name the person as a whole word."""
    names = [p.casefold().strip() for p in persons if p and p.strip()]
    if not names:
        return False
    tagged = [
        t[len(PERSON_TAG_PREFIX) :].casefold().strip()
        for t in record.tags
        if t.casefold().startswith(PERSON_TAG_PREFIX)
    ]
    if tagged:
        return any(n == v or _words(n) <= _words(v) for n in names for v in tagged)
    if not record.entity:
        return False
    entity = _words(record.entity)
    return any(_words(n) and _words(n) <= entity for n in names)


def person_time_leg(
    records: Iterable[MemoryRecord],
    persons: Sequence[str],
    span: tuple[datetime, datetime] | None,
    top_k: int,
) -> list[LegHit]:
    """#36: records in ``span`` and/or about ``persons``, best ``top_k`` first.

    Records matching both come first, then records in the span, then records about a
    person; within each group the one closest to the span's middle, else the newest.
    Ties break on ``record_id``, so the order is deterministic."""
    if not persons and span is None:
        return []
    mid = span[0] + (span[1] - span[0]) / 2 if span is not None else None
    ranked: list[tuple[tuple[int, int, float, float, str], str]] = []
    for record in records:
        when = _aware(record.valid_from)
        inside = span is not None and span[0] <= when < span[1]
        about = bool(persons) and person_matches(record, persons)
        hits = int(inside) + int(about)
        if hits == 0:
            continue
        distance = abs((when - mid).total_seconds()) if inside and mid is not None else 0.0
        key = (-hits, int(not inside), distance, -when.timestamp(), record.record_id)
        ranked.append((key, record.record_id))
    ranked.sort(key=lambda pair: pair[0])
    return [LegHit(record_id, 1.0) for _, record_id in ranked[:top_k]]
