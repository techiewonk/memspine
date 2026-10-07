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
from memspine.core.temporal_resolve import WeekMode, resolve

__all__ = [
    "RECOMMENDATION_TAG",
    "SPEAKER_PREFIX",
    "LegHit",
    "asks_about_assistant",
    "assistant_leg",
    "is_recommendation",
    "metadata_leg",
    "query_interval",
    "speaker_leg",
    "speaker_of",
    "temporal_leg",
]

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


def query_interval(
    query: str, anchor: datetime | None = None, *, week: WeekMode = "calendar"
) -> tuple[datetime, datetime] | None:
    """The first absolute ``[start, end)`` span named in ``query``, or None.

    F2 (plan v3.2, ``read.temporal_relative``): with ``anchor`` (the read time, or an
    explicit as-of), a query with no absolute date falls back to its first relative
    phrase ("last week", "two months ago", "yesterday") resolved against ``anchor`` by
    the H1 rules. Without ``anchor`` a relative phrase names no span (unchanged)."""
    span = _absolute_interval(query)
    if span is not None or anchor is None:
        return span
    found = resolve(query, anchor, week=week)
    if not found:
        return None
    first, last = found[0].first, found[0].last
    start = datetime(first.year, first.month, first.day, tzinfo=UTC)
    end = datetime(last.year, last.month, last.day, tzinfo=UTC) + timedelta(days=1)
    return start, end


def _absolute_interval(query: str) -> tuple[datetime, datetime] | None:
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
    query: str,
    records: Iterable[MemoryRecord],
    top_k: int,
    *,
    event_dates: bool = False,
    anchor: datetime | None = None,
    week: WeekMode = "calendar",
) -> list[LegHit]:
    """Records whose event time lies in the query's span, closest to its middle first.

    ``event_dates`` (B2, ``read.temporal_leg_event_dates``): a record whose
    ``happened:`` date (a mined fact's event date) overlaps the span also enters, at
    the start of the overlap, so an event said days after it happened is found by its
    own date. A record matching both ways keeps the closer time. Ties: ``chrono_key``.
    ``anchor`` / ``week`` (F2): see :func:`query_interval`.
    """
    span = query_interval(query, anchor, week=week)
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


#: W8 (plan v3.2): the speaker of a stored turn written as "Name: text".
SPEAKER_PREFIX = "speaker:"
_SPEAKER_LINE = re.compile(r"^\s*(?P<name>[A-Z][\w'-]{1,30}(?: [A-Z][\w'-]{1,30})?):\s+\S")


def speaker_of(content: str) -> str | None:
    """The speaker name a turn's text opens with ("Caroline: ..." -> "caroline")."""
    m = _SPEAKER_LINE.match(content)
    return m["name"].lower() if m else None


def speaker_leg(query: str, records: Iterable[MemoryRecord], top_k: int) -> list[LegHit]:
    """W8 (``read.subject_leg``): the turns of every speaker the query names (whole
    word, case-insensitive), ranked by content-word overlap with the query, then
    newest first. Empty when the query names no known speaker."""
    from memspine.core.query_shape import content_words

    text = query.lower()
    by_speaker: dict[str, list[MemoryRecord]] = {}
    for r in records:
        for tag in r.tags:
            if tag.startswith(SPEAKER_PREFIX):
                by_speaker.setdefault(tag[len(SPEAKER_PREFIX) :], []).append(r)
    named = [
        r
        for name, turns in by_speaker.items()
        if re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text)
        for r in turns
    ]
    if not named:
        return []
    wanted = content_words(query)
    named.sort(
        key=lambda r: (
            -len(wanted & content_words(r.content)),
            -_aware(r.valid_from).timestamp(),
            chrono_key(r),
        )
    )
    return [LegHit(r.record_id, 1.0) for r in named[:top_k]]


#: W11 (plan v3.2): the question asks what the ASSISTANT said ("what did you
#: recommend", "you told me", "your suggestion").
_ASKS_ASSISTANT = re.compile(
    r"\byou (?:say|said|tell|told|recommend(?:ed)?|suggest(?:ed)?|mention(?:ed)?|give|gave|"
    r"write|wrote|list(?:ed)?|advised?|proposed?|shared?)\b|\byour (?:recommendation|suggestion|advice|answer|list|tip)s?\b",
    re.IGNORECASE,
)
#: W11: an assistant turn that recommends something (tagged ``recommendation``).
_RECOMMENDS = re.compile(
    r"\bI(?: would|'d)? (?:recommend|suggest)\b|\byou (?:should|could|might) (?:try|check out|"
    r"read|watch|visit|listen to|consider)\b|\bmy (?:top )?(?:recommendation|pick|suggestion)s?\b",
    re.IGNORECASE,
)
RECOMMENDATION_TAG = "recommendation"


def asks_about_assistant(query: str) -> bool:
    """W11: True when the question is about what the assistant said or recommended."""
    return bool(_ASKS_ASSISTANT.search(query))


def is_recommendation(text: str) -> bool:
    """W11: True when an assistant turn recommends something."""
    return bool(_RECOMMENDS.search(text))


def assistant_leg(query: str, records: Iterable[MemoryRecord], top_k: int) -> list[LegHit]:
    """W11 (``read.role_aware``): for a question about what the assistant said, the
    assistant's own turns (recommendations first), by content-word overlap with the
    question, then newest first. Empty for any other question."""
    from memspine.core.query_shape import content_words

    if not asks_about_assistant(query):
        return []
    wanted = content_words(query)
    turns = [r for r in records if r.source.role == "assistant"]
    turns.sort(
        key=lambda r: (
            RECOMMENDATION_TAG not in r.tags,
            -len(wanted & content_words(r.content)),
            -_aware(r.valid_from).timestamp(),
            chrono_key(r),
        )
    )
    return [LegHit(r.record_id, 1.0) for r in turns[:top_k]]
