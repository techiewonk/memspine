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

from memspine.config.constants import MENTION_CACHE_MAX, TEMPORAL_SOFT_MARGIN_DAYS
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
    "sentence_leg",
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

# N45: dates with no year. Case-sensitive capitalised month names, next to a day
# number or after a preposition, so "may I" or "march on" never match.
_CAP_MON = (
    r"(?P<mon>"
    + "|".join(
        sorted(
            {n for n in calendar.month_name if n} | {n for n in calendar.month_abbr if n},
            key=len,
            reverse=True,
        )
    )
    + r")\.?"
)
_NO_YEAR = r"(?!\s*,?\s*(?:19|20)\d{2})"
_DAY_MON = re.compile(rf"\b(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_CAP_MON}{_NO_YEAR}")
_MON_DAY = re.compile(rf"\b{_CAP_MON}\s+(?P<d>\d{{1,2}})(?:st|nd|rd|th)?\b{_NO_YEAR}")
_IN_MON = re.compile(rf"\b(?:in|during|since|by|early|late|mid-?)\s+{_CAP_MON}\b{_NO_YEAR}")


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
    query: str,
    anchor: datetime | None = None,
    *,
    week: WeekMode = "calendar",
    year_ref: datetime | None = None,
) -> tuple[datetime, datetime] | None:
    """The first absolute ``[start, end)`` span named in ``query``, or None.

    N45 (``read.temporal_infer_year``): with ``year_ref`` (the newest record time, or
    an explicit as-of), a date with no year ("on 7 May", "in March") takes the latest
    year that puts it on or before ``year_ref``.

    F2 (plan v3.2, ``read.temporal_relative``): with ``anchor`` (the read time, or an
    explicit as-of), a query with no absolute date falls back to its first relative
    phrase ("last week", "two months ago", "yesterday") resolved against ``anchor`` by
    the H1 rules. Without ``anchor`` a relative phrase names no span (unchanged)."""
    span = _absolute_interval(query)
    if span is None and year_ref is not None:
        span = _yearless_interval(query, year_ref)
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


def _yearless_interval(query: str, ref: datetime) -> tuple[datetime, datetime] | None:
    """N45: "7 May" / "May 7th" (a day) or "in May" (a month) with the year inferred
    as the latest one that puts the date on or before ``ref``."""
    ref = _aware(ref)
    for pattern in (_DAY_MON, _MON_DAY):
        if m := pattern.search(query):
            mon, d = _MONTHS[m["mon"].lower()], int(m["d"])
            for y in (ref.year, ref.year - 1):
                day = _day(y, mon, d)
                if day is not None and day[0] <= ref:
                    return day
            return None
    if m := _IN_MON.search(query):
        mon = _MONTHS[m["mon"].lower()]
        y = ref.year if mon <= ref.month else ref.year - 1
        start = datetime(y, mon, 1, tzinfo=UTC)
        return start, datetime(y + (mon == 12), mon % 12 + 1, 1, tzinfo=UTC)
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
    year_ref: datetime | None = None,
    rank: str = "midpoint",
    soft: bool = False,
    mentions: bool = False,
) -> list[LegHit]:
    """Records whose event time lies in the query's span, closest to its middle first.

    ``event_dates`` (B2, ``read.temporal_leg_event_dates``): a record whose
    ``happened:`` date (a mined fact's event date) overlaps the span also enters, at
    the start of the overlap, so an event said days after it happened is found by its
    own date. A record matching both ways keeps the closer time. Ties: ``chrono_key``.
    ``anchor`` / ``week`` (F2) and ``year_ref`` (N45): see :func:`query_interval`.

    ``rank`` (N61, ``read.temporal_rank``): ``midpoint`` (unchanged) or ``overlap``:
    in-span records with more content words shared with the query first, then by
    closeness to the middle. ``soft`` (N44, ``read.temporal_soft``): when fewer than
    ``top_k`` records fall in the span, the rest of the leg is filled with the records
    nearest outside it, within one span length (at least ``TEMPORAL_SOFT_MARGIN``).

    ``mentions`` (N30, ``read.temporal_leg_mentions``): a turn whose text names a date
    ("last weekend", "on 7 May", "yesterday"), resolved against the turn's own time,
    also enters when that date overlaps the span, so "I went camping last weekend"
    said on 8 May is found for "7 May". Works on raw turns, unlike ``event_dates``.
    """
    records = list(records)
    span = query_interval(query, anchor, week=week, year_ref=year_ref)
    if span is None:
        return []
    start, end = span
    mid = start + (end - start) / 2
    asked = _content_words(query) if rank == "overlap" else frozenset()
    scored: list[tuple[float, float, MemoryRecord]] = []
    for r in records:
        times = []
        said = _aware(r.valid_from)
        if start <= said < end:
            times.append(said)
        if event_dates and (at := _happened_in(r, start, end)) is not None:
            times.append(at)
        if mentions:
            for lo, hi in mentioned_spans(r):
                if max(lo, start) < min(hi, end):
                    times.append(max(lo, start))
        if times:
            shared = len(asked & _content_words(r.content)) if asked else 0
            dist = min(abs((t - mid).total_seconds()) for t in times)
            scored.append((-shared, dist, r))
    scored.sort(key=lambda item: (item[0], item[1], chrono_key(item[2])))
    if soft and len(scored) < top_k:
        inside = {r.record_id for _, _, r in scored}
        margin = max(end - start, TEMPORAL_SOFT_MARGIN)
        near: list[tuple[float, MemoryRecord]] = []
        for r in records:
            said = _aware(r.valid_from)
            if r.record_id in inside or not (start - margin <= said < end + margin):
                continue
            gap = (start - said) if said < start else (said - end)
            near.append((gap.total_seconds(), r))
        near.sort(key=lambda pair: (pair[0], chrono_key(pair[1])))
        scored += [(0, 0.0, r) for _, r in near[: top_k - len(scored)]]
    return [LegHit(r.record_id, 1.0) for _, _, r in scored[:top_k]]


#: N44: the smallest widening of a soft temporal span.
TEMPORAL_SOFT_MARGIN = timedelta(days=TEMPORAL_SOFT_MARGIN_DAYS)


#: N30: date spans each record's text names, by (record_id, content); bounded.
_MENTIONS: dict[tuple[str, str], tuple[tuple[datetime, datetime], ...]] = {}


def mentioned_spans(record: MemoryRecord) -> tuple[tuple[datetime, datetime], ...]:
    """N30: the date spans a record's text names, resolved against its own time.

    Relative phrases ("last weekend", "yesterday", "two weeks ago") use the H1 rules;
    an absolute or yearless date ("7 May 2023", "on 7 May") is read as in a query.
    Cached by record id and content (records are immutable; the cache is bounded).
    """
    key = (record.record_id, record.content)
    cached = _MENTIONS.get(key)
    if cached is not None:
        return cached
    said = _aware(record.valid_from)
    spans: list[tuple[datetime, datetime]] = []
    for found in resolve(record.content, said):
        lo = datetime(found.first.year, found.first.month, found.first.day, tzinfo=UTC)
        hi = datetime(found.last.year, found.last.month, found.last.day, tzinfo=UTC)
        spans.append((lo, hi + timedelta(days=1)))
    named = _absolute_interval(record.content) or _yearless_interval(record.content, said)
    if named is not None:
        spans.append(named)
    if len(_MENTIONS) >= MENTION_CACHE_MAX:
        _MENTIONS.clear()
    result = _MENTIONS[key] = tuple(spans)
    return result


def _content_words(text: str) -> frozenset[str]:
    from memspine.core.query_shape import content_words

    return content_words(text)


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
    r"write|wrote|list(?:ed)?|advised?|proposed?|shared?)\b"
    r"|\byour (?:recommendation|suggestion|advice|answer|list|tip)s?\b",
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


_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")


def sentence_leg(query: str, records: Iterable[MemoryRecord], top_k: int) -> list[LegHit]:
    """N05 (plan v3.2, EverMemOS ``amaxsim``, lexical): records ranked by their single
    best-matching sentence (shared content words over the square root of the
    sentence's length), so one strongly matching sentence in a long turn is not
    diluted by the rest of it. Records sharing no word with the query are left out."""
    import math

    from memspine.core.query_shape import content_words

    wanted = content_words(query)
    if not wanted:
        return []
    scored: list[tuple[float, MemoryRecord]] = []
    for r in records:
        best = 0.0
        for sentence in _SENTENCE.split(r.content):
            words = content_words(sentence)
            if words:
                best = max(best, len(wanted & words) / math.sqrt(len(words)))
        if best > 0:
            scored.append((best, r))
    scored.sort(key=lambda pair: (-pair[0], chrono_key(pair[1])))
    return [LegHit(r.record_id, 1.0) for _, r in scored[:top_k]]
