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
from memspine.core.language import blocked
from memspine.core.records import MemoryRecord, chrono_key
from memspine.core.temporal_resolve import WeekMode, resolve

__all__ = [
    "RECOMMENDATION_TAG",
    "SPEAKER_PREFIX",
    "LegHit",
    "NamedLeg",
    "asks_about_assistant",
    "assistant_leg",
    "bridge_phrases",
    "cohesion_leg",
    "comparison_speaker_legs",
    "date_words",
    "entity_expand_leg",
    "entity_leg",
    "forget_mentions",
    "has_bridge_cue",
    "is_recommendation",
    "metadata_leg",
    "named_terms",
    "query_interval",
    "question_subject",
    "sentence_leg",
    "sentences",
    "speaker_leg",
    "speaker_of",
    "speaker_vector_leg",
    "subject_vector_leg",
    "temporal_leg",
    "view_tag_leg",
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
    if blocked(query):  # I23: month names / phrases are English; only an ISO date stays
        m = _ISO.search(query)
        return _day(int(m["y"]), int(m["m"]), int(m["d"])) if m else None
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


def _speaker_name(record: MemoryRecord) -> str | None:
    """The speaker of a turn: its ``speaker:`` tag, else the "Name:" prefix of its text."""
    for tag in record.tags:
        if tag.startswith(SPEAKER_PREFIX):
            return tag[len(SPEAKER_PREFIX) :]
    return speaker_of(record.content)


def speaker_vector_leg(
    query: str,
    records: Iterable[MemoryRecord],
    vector_hits: Iterable[LegHit],
    top_k: int = 30,
) -> list[LegHit]:
    """B1 (``read.list_mode``): the first ``top_k`` ``vector_hits`` (in vector order, scores
    kept) spoken by the one speaker the query names (whole word, case-insensitive; "Melanie's
    kids" names Melanie). The speaker of a record is its ``speaker:`` tag, else the "Name:"
    prefix of its text. Empty when the query names no known speaker or more than one."""
    by_id = {r.record_id: r for r in records}
    names = {name for r in by_id.values() if (name := _speaker_name(r))}
    text = query.lower()
    named = {n for n in names if re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text)}
    if len(named) != 1:
        return []
    [who] = named
    out: list[LegHit] = []
    for hit in vector_hits:
        rec = by_id.get(hit.record_id)
        if rec is not None and _speaker_name(rec) == who:
            out.append(hit)
            if len(out) >= top_k:
                break
    return out


#: I5: a "user:" / "assistant:" text prefix (loaders that fold the role into the text).
_ROLE_PREFIX = re.compile(r"^\s*(user|assistant)\s*:\s", re.I)
_FIRST_PERSON = re.compile(r"\b(?:i|my|me|mine|myself)\b", re.I)
_SECOND_PERSON = re.compile(r"\b(?:you|your|yours|yourself|you're|you've)\b", re.I)


def _chat_role(record: MemoryRecord) -> str | None:
    """I5: ``user`` / ``assistant`` for a turn: a "user:" / "assistant:" text prefix, else the
    record's source role when that is one of the two. None for system / tool / other."""
    m = _ROLE_PREFIX.match(record.content)
    role = m[1].lower() if m else (record.source.role or "").lower()
    return role if role in ("user", "assistant") else None


def question_subject(query: str) -> str | None:
    """I5: the chat role a question's subject points at: a first-person "I / my / me" -> ``user``;
    a second-person "you / your" -> ``assistant``. Both, or neither (a pronoun "she / they",
    a named person) -> None: unresolved, no vote."""
    first = bool(_FIRST_PERSON.search(query))
    second = bool(_SECOND_PERSON.search(query))
    if first == second:
        return None
    return "user" if first else "assistant"


def subject_vector_leg(
    query: str,
    records: Iterable[MemoryRecord],
    vector_hits: Iterable[LegHit],
    top_k: int = 30,
) -> list[LegHit]:
    """I5 (``read.speaker_vote_mode="subject"``): the speaker vote keyed on the question's
    subject. A named person who is a known speaker (``speaker:`` tag, else the "Name:" prefix)
    votes as :func:`speaker_vector_leg` does. With no named speaker, a first-person subject
    votes on the ``user`` turns and a second-person subject on the ``assistant`` turns (a
    "user:" / "assistant:" text prefix, else the record's source role), but only when the
    store holds turns of both roles; otherwise the vote could not tell turns apart. A pronoun
    ("she", "they") or a mixed subject is unresolved and casts no vote."""
    recs = list(records)
    hits = list(vector_hits)
    named = speaker_vector_leg(query, recs, hits, top_k)
    if named:
        return named
    text = query.lower()
    names = {n for r in recs if (n := _speaker_name(r)) and n not in ("user", "assistant")}
    if any(re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text) for n in names):
        return []  # a known speaker is named (alone with no hits, or one of several)
    target = question_subject(query)
    if target is None:
        return []
    roles = {_chat_role(r) for r in recs} - {None}
    if len(roles) < 2:
        return []
    by_id = {r.record_id: r for r in recs}
    out: list[LegHit] = []
    for hit in hits:
        rec = by_id.get(hit.record_id)
        if rec is not None and _chat_role(rec) == target:
            out.append(hit)
            if len(out) >= top_k:
                break
    return out


#: R2-2: a comparison cue in a question that names two speakers.
_COMPARISON = re.compile(
    r"\bboth\b|\bin common\b|\beach\b|\bshare[sd]?\b|\bsimilar(?:ly|ities)?\b|\balike\b",
    re.I,
)


def comparison_speaker_legs(
    query: str,
    records: Iterable[MemoryRecord],
    vector_hits: Iterable[LegHit],
    top_k: int = 30,
) -> list[tuple[str, list[LegHit]]]:
    """R2-2 (``read.list_mode``): for a question naming exactly TWO known speakers with a
    comparison cue ("both", "in common", "each"), one vote per speaker: ``[(name, hits)]``
    in the order the names occur in the query, each as :func:`speaker_vector_leg` would
    build for a one-speaker question. Empty otherwise."""
    if not _COMPARISON.search(query):
        return []
    recs = list(records)
    hits = list(vector_hits)
    names = {name for r in recs if (name := _speaker_name(r))}
    text = query.lower()
    found = []
    for n in names:
        m = re.search(rf"(?<!\w){re.escape(n)}(?!\w)", text)
        if m:
            found.append((m.start(), n))
    if len(found) != 2:
        return []
    by_id = {r.record_id: r for r in recs}
    out: list[tuple[str, list[LegHit]]] = []
    for _, who in sorted(found):
        votes: list[LegHit] = []
        for hit in hits:
            rec = by_id.get(hit.record_id)
            if rec is not None and _speaker_name(rec) == who:
                votes.append(hit)
                if len(votes) >= top_k:
                    break
        out.append((who, votes))
    return out


_BRIDGE_WORD = re.compile(r"[A-Za-z][A-Za-z'-]*")
_BRIDGE_SPEAKER = re.compile(r"^\s*[\w .'-]{1,40}:\s")
_BRIDGE_FILLER = frozenset(
    "really very just also still much many some going been being were was are have has had "  # noqa: SIM905
    "would could should will can get got gets getting like love loved want wanted yeah thanks "
    "thank great good nice wow oh well that this these those there here what when where who "
    "four three five six seven eight nine ten twice once ago year years month months week "
    "weeks day days time times today yesterday tomorrow how why then than them they their "
    "your you our its it's i'm i've don't didn't".split()
)

_CUE_REL = (
    r"(?:son|daughter|kid|child|husband|wife|spouse|partner|boyfriend|girlfriend|fianc\w+|"
    r"brother|sister|mother|father|mom|dad|parent|grand\w+|cousin|aunt|uncle|friend|boss|"
    r"colleague|neighbou?r|roommate|teacher|coach|mentor)"
)
_CUE_THING = (
    r"(?:city|town|place|country|state|school|company|store|shop|restaurant|studio|caf[eé]|"
    r"hospital|neighbou?rhood|village|house|gym|club|group|event|trip|job|business|app|book|"
    r"movie|film|song|show|game|album|podcast|article|gift|painting|poem|class|course|"
    r"festival|concert)"
)
_BRIDGE_CUE = re.compile(
    r"\bhome ?(?:country|town|city|state|land)\b|\bhometown\b|\bnative (?:country|city|town|land)\b"
    r"|\bwhere\b[^?]*\b(?:mov(?:e|ed|es|ing)|relocat\w+|came|come|grew|grow\w*|originally)\b"
    r"[^?]*\bfrom\b"
    rf"|\bthe {_CUE_THING}\s+(?:where|in which)\b"
    rf"|\bthe {_CUE_THING}\s+(?:that|which)\s+(?:[A-Z]\w+|he|she|they)\s+"
    r"(?:(?:has|had|have|did)\s+)?(?:recommend\w*|suggest\w*|mention\w*|read|watch\w*|"
    r"visit\w*|attend\w*|join\w*|bought|got|gave|made|painted|wrote|opened|started|"
    r"founded|moved)\b"
    rf"|\b(?:her|his|their|my|\w+'s)\s+{_CUE_REL}'s?\b",
    re.IGNORECASE,
)


def has_bridge_cue(question: str) -> bool:
    """R2-1b (``read.bridge_hop_gate``): the question describes its answer's entity instead
    of naming it ("home country", "where ... move from", "the studio that X opened", a
    possessive + relation noun like "her son's"), so a second hop may be needed to find
    the entity first. Deliberately conservative: it fires on about 1 of 80 LoCoMo
    questions."""
    return bool(_BRIDGE_CUE.search(question))


def bridge_phrases(question: str, texts: Iterable[str], limit: int = 3) -> list[str]:
    """R2-1 (``read.bridge_hop``): up to ``limit`` key noun phrases of the first-hop hit
    texts that the question does not contain. A phrase is the first two words of a run of adjacent
    content words (stop words, mid-sentence names and punctuation break a run), so "moved
    from my home country" gives "home country". Most frequent first, then first seen.
    Deterministic."""
    from memspine.core.query_shape import _STOP

    asked = {w.lower() for w in _BRIDGE_WORD.findall(question)}
    counts: dict[str, int] = {}
    order: dict[str, int] = {}
    for text in texts:
        body = _BRIDGE_SPEAKER.sub("", text, count=1)
        for sentence in re.split(r"[.!?;,:\n()\"]+", body):
            run: list[str] = []
            raw = _BRIDGE_WORD.findall(sentence)
            for i, word in enumerate([*raw, ""]):
                low = word.lower()
                keep = (
                    bool(word)
                    and len(low) > 2
                    and low not in _STOP
                    and low not in _BRIDGE_FILLER
                    and not (word[0].isupper() and i > 0)  # a name inside a sentence
                )
                if keep:
                    run.append(low)
                    continue
                if len(run) >= 2:
                    phrase = " ".join(run[:2])
                    if not any(w in asked for w in run[:2]):
                        counts[phrase] = counts.get(phrase, 0) + 1
                        order.setdefault(phrase, len(order))
                run = []
    ranked = sorted(counts, key=lambda p: (-counts[p], order[p]))
    return ranked[:limit]


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


#: N59: capitalised words that open questions or name times, never entities.
_NOT_NAMES = frozenset(
    {
        "when",
        "what",
        "where",
        "which",
        "who",
        "whom",
        "whose",
        "why",
        "how",
        "did",
        "does",
        "do",
        "is",
        "are",
        "was",
        "were",
        "has",
        "have",
        "had",
        "will",
        "would",
        "can",
        "could",
        "i",
        "the",
        "a",
        "an",
        "in",
        "on",
        "at",
        "of",
        "and",
        "or",
        "my",
        "your",
        "tell",
        "list",
        "name",
        "describe",
    }
    | {n.lower() for n in calendar.month_name if n}
    | {n.lower() for n in calendar.day_name}
)
_CAP_WORD = re.compile(r"\b[A-Z][a-zA-Z'-]{1,}\b")
_YEAR_WORD = re.compile(r"\b(?:19|20)\d{2}\b")
#: G-7: the first word of each sentence (turn start, after ".!?", or after "Name: ").
_SENTENCE_START = re.compile(r"((?:^[A-Z][\w'-]{1,30}:\s+|^|[.!?]\s+)[\"'(]?)([A-Z][a-z]+)")


def named_terms(
    query: str, exclude: Iterable[str] = (), *, skip_sentence_start: bool = False
) -> list[str]:
    """N59: proper nouns and years a question names, lower-cased, in order.

    Capitalised words that are not question words, months or weekdays, plus four-digit
    years; ``exclude`` drops names already handled elsewhere (the conversation's
    speakers, which the subject leg covers). A possessive "'s" is stripped.

    ``skip_sentence_start`` (G-7, statements): a capital that only opens a sentence
    ("Yes", "So", "Well") is not taken as a name."""
    skip = {e.lower() for e in exclude} | _NOT_NAMES
    terms: list[str] = []
    if skip_sentence_start:
        query = _SENTENCE_START.sub(lambda m: m.group(1) + m.group(2).lower(), query)
    for word in _CAP_WORD.findall(query):
        w = word.lower().removesuffix("'s").strip("'-")
        if w and w not in skip and w not in terms:
            terms.append(w)
    for year in _YEAR_WORD.findall(query):
        if year not in terms:
            terms.append(year)
    return terms


def entity_leg(
    query: str, records: Iterable[MemoryRecord], top_k: int, exclude: Iterable[str] = ()
) -> list[LegHit]:
    """N59 (``read.entity_leg``, Dakera name boost): raw records whose text names the
    question's proper nouns or years (whole word, case-insensitive), most names
    matched first, then most content words shared with the question, then time order.
    Empty when the question names nothing beyond ``exclude`` (the speakers)."""
    names = named_terms(query, exclude)
    if not names:
        return []
    patterns = [re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.I) for n in names]
    asked = _content_words(query)
    scored: list[tuple[int, int, MemoryRecord]] = []
    for r in records:
        hits = sum(1 for p in patterns if p.search(r.content))
        if hits:
            scored.append((-hits, -len(asked & _content_words(r.content)), r))
    scored.sort(key=lambda item: (item[0], item[1], chrono_key(item[2])))
    return [LegHit(r.record_id, 1.0) for _, _, r in scored[:top_k]]


class NamedLeg(list[LegHit]):
    """N62: a fused leg that carries its name, so per-shape weights can find it."""

    def __init__(self, name: str, hits: Iterable[LegHit] = ()) -> None:
        super().__init__(hits)
        self.name = name


def cohesion_leg(
    anchors: list[MemoryRecord], records: Iterable[MemoryRecord], top_k: int, window: timedelta
) -> list[LegHit]:
    """N43 (``read.cohesion_leg``, Dakera session cohesion): records said within
    ``window`` of an anchor hit (the first-pass top hits), nearest first; the anchors
    themselves are left out (they already rank)."""
    ids = {a.record_id for a in anchors}
    times = [_aware(a.valid_from) for a in anchors]
    near: list[tuple[float, MemoryRecord]] = []
    for r in records:
        if r.record_id in ids:
            continue
        said = _aware(r.valid_from)
        gap = min((abs((said - t).total_seconds()) for t in times), default=None)
        if gap is not None and gap <= window.total_seconds():
            near.append((gap, r))
    near.sort(key=lambda pair: (pair[0], chrono_key(pair[1])))
    return [LegHit(r.record_id, 1.0) for _, r in near[:top_k]]


def entity_expand_leg(
    anchors: list[MemoryRecord],
    records: Iterable[MemoryRecord],
    top_k: int,
    exclude: Iterable[str] = (),
    max_share: float = 1.0,
) -> list[LegHit]:
    """N32 (``read.entity_expand_leg``; Hindsight / EverMemOS entity links): records
    naming the proper nouns and years the anchor hits name, most shared names first.

    N53 (Mem0 entity damping): a name found in more than ``max_share`` of the records
    is too common to expand on and is dropped. ``exclude`` drops the speakers."""
    records = list(records)
    names: list[str] = []
    for a in anchors:
        for n in named_terms(a.content, exclude):
            if n not in names:
                names.append(n)
    if not names or not records:
        return []
    patterns = {n: re.compile(rf"(?<!\w){re.escape(n)}(?!\w)", re.I) for n in names}
    holders = {n: [r for r in records if p.search(r.content)] for n, p in patterns.items()}
    kept = [n for n in names if len(holders[n]) / len(records) <= max_share]
    ids = {a.record_id for a in anchors}
    counts: dict[str, int] = {}
    by_id = {r.record_id: r for r in records}
    for n in kept:
        for r in holders[n]:
            if r.record_id not in ids:
                counts[r.record_id] = counts.get(r.record_id, 0) + 1
    ranked = sorted(counts, key=lambda rid: (-counts[rid], chrono_key(by_id[rid])))
    return [LegHit(rid, 1.0) for rid in ranked[:top_k]]


def sentences(text: str, min_words: int = 3) -> list[str]:
    """N60: the sentences of ``text`` (the N05 splitter), dropping fragments under
    ``min_words`` words."""
    return [s.strip() for s in _SENTENCE.split(text) if len(s.split()) >= min_words]


def forget_mentions(texts: Iterable[str]) -> None:
    """N30 / #43: drop the cached date spans of erased texts (the cache keys hold them)."""
    erased = set(texts)
    for key in [k for k in _MENTIONS if k[1] in erased]:
        del _MENTIONS[key]


def date_words(record: MemoryRecord) -> str:
    """N30 (write side, ``read.lexical_dates``): the words of the dates a record was
    said on and names, for the BM25 index: "2023-05-07 7 May 2023 Sunday" per day."""
    days = [_aware(record.valid_from)]
    days += [lo for lo, _ in mentioned_spans(record)]
    seen: list[str] = []
    for d in days:
        words = f"{d:%Y-%m-%d} {d.day} {d:%B} {d.year} {d:%A}"
        if words not in seen:
            seen.append(words)
    return " ".join(seen)


def view_tag_leg(query: str, records: Iterable[MemoryRecord], top_k: int) -> list[LegHit]:
    """G-16 (SimpleMem symbolic leg, ``read.view_tag_leg``): records whose location,
    topic or person view tags (``loc:`` / ``topic:`` / ``person:``, written by
    ``consolidation.mine_multiview``) share words with the question, most shared words
    first, then location over topic over person, then time order."""
    from memspine.core.fact_views import LOCATION_PREFIX, PERSON_PREFIX, TOPIC_PREFIX

    asked = _content_words(query)
    if not asked:
        return []
    weight = {LOCATION_PREFIX: 3, TOPIC_PREFIX: 2, PERSON_PREFIX: 1}
    scored: list[tuple[int, int, MemoryRecord]] = []
    for r in records:
        best_words, best_kind = 0, 0
        for tag in r.tags:
            for prefix, kind in weight.items():
                if tag.startswith(prefix):
                    shared = len(asked & _content_words(tag[len(prefix) :]))
                    if (shared, kind) > (best_words, best_kind) and shared:
                        best_words, best_kind = shared, kind
        if best_words:
            scored.append((-best_words, -best_kind, r))
    scored.sort(key=lambda item: (item[0], item[1], chrono_key(item[2])))
    return [LegHit(r.record_id, 1.0) for _, _, r in scored[:top_k]]
