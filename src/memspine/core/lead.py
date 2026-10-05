"""H22 (Mnemon): the lead section of an assembled context, as pure functions.

Two blocks open the context when enabled: the preferences and standing requests
the user stated, and one dated timeline per topic entity of the retrieved facts.
Both are read-time projections over stored records: deterministic, no model, and
never written back. The engine decides which records may appear (status,
quarantine, admission, trust); this module only recognises and renders.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime

from memspine.config import constants
from memspine.core.escaping import escape_markers
from memspine.core.event_date import date_anchor, happened_of, label_start
from memspine.core.query_shape import core_terms
from memspine.core.records import MemoryRecord
from memspine.core.temporal_resolve import WeekMode, resolve

__all__ = [
    "card_line",
    "count_terms",
    "distinct_occurrences",
    "event_day",
    "graph_fact_line",
    "is_standing_instruction",
    "mentions_any",
    "mentions_event",
    "occurrence_line",
    "query_names",
    "render_graph_facts",
    "render_occurrences",
    "render_profile",
    "render_standing",
    "render_timeline",
    "timeline_line",
]

#: Cue phrases of a standing request or stated preference. Deliberately narrow:
#: "I always loved painting" is a fact about the past, not a request, so a bare
#: "always" never matches; it must govern a verb aimed at the assistant.
_STANDING = re.compile(
    r"\b(?:"
    r"from now on|going forward|in (?:the )?future,? (?:please|always|never|don't|do not)"
    r"|please (?:always|never|remember|don't|do not|stop|keep|use|call|address)"
    r"|(?:always|never) (?:use|call|say|answer|reply|respond|write|give|include|"
    r"mention|remind|show|send|format|address|refer)"
    r"|(?:don't|do not) (?:ever )?(?:call|mention|say|use|remind|send|share|bring up)"
    r"|remember (?:that|to) (?:i|my|me|always|never)"
    r"|i(?:'d| would)? prefer|i like it when you|i don't like it when you"
    r"|call me|my preference is|my preferred"
    r")\b",
    re.IGNORECASE,
)


def is_standing_instruction(text: str) -> bool:
    """True when ``text`` states a preference or a request that should persist."""
    return bool(_STANDING.search(text))


def _strip_entity(content: str, entity: str) -> str:
    """``"Caroline hobby: painting"`` under entity Caroline reads ``"hobby: painting"``."""
    head = content[: len(entity)]
    if head.lower() == entity.lower() and content[len(entity) : len(entity) + 1] in (" ", ":"):
        return content[len(entity) :].lstrip(" :")
    return content


def timeline_line(record: MemoryRecord, entity: str, until: datetime | None = None) -> str:
    """One dated timeline entry; a superseded or ended fact shows its end date."""
    text = escape_markers(_strip_entity(" ".join(record.content.split()), entity))
    line = f"- {record.valid_from:%Y-%m-%d}: {text}"
    if until is not None:
        line += f" (until {until:%Y-%m-%d})"
    return line


def card_line(
    record: MemoryRecord,
    said: datetime | None = None,
    *,
    claim: bool = False,
    happened: str | None = None,
) -> str:
    """G1b: one card, ``[said YYYY-MM-DD] Entity: fact``.

    #29: ``happened`` (a mined fact's happened date, ``read.cards_event_date``) that
    is not the day it was said renders ``[said YYYY-MM-DD · happened <date>]``.

    A mined fact is stored as ``"<entity> <attribute>: <statement>"``; the card
    keeps the entity and the statement. Content without that shape (a wrapped
    low-trust or instruction-flagged fact) is shown whole. ``said`` is the date the
    fact was said (its earliest source turn); without one the card has no date,
    since the block's marker reads every date as "when it was said" and the
    fact's own ``valid_from`` is the event date. ``claim`` (B9) prefixes
    :data:`constants.CLAIM_MARKER`. A #30 list card is shown whole, with no date.
    """
    text = " ".join(record.content.split())
    if constants.LIST_CARD_TAG in record.tags:
        # #30: a list card spans many dates (each item carries its own): it is
        # shown whole (already escaped and wrapped by the engine), with no said date.
        return f"{constants.CLAIM_MARKER} {text}" if claim else text
    if record.entity:
        rest = _strip_entity(text, record.entity)
        if rest != text and ": " in rest:
            text = f"{record.entity}: {rest.split(': ', 1)[1]}"
    if claim:
        text = f"{constants.CLAIM_MARKER} {text}"
    if said is not None:
        # The date the fact was SAID (its earliest source turn): the miner's event
        # date is unreliable, and the source turn carries the resolved event date.
        if happened and happened != f"{said:%Y-%m-%d}":
            return f"[said {said:%Y-%m-%d} · happened {happened}] {text}"
        return f"[said {said:%Y-%m-%d}] {text}"
    return text


#: Capitalised words that open or join a question, never a person's name.
_NOT_NAMES = frozenset(
    [
        "what",
        "when",
        "where",
        "who",
        "whom",
        "whose",
        "why",
        "how",
        "which",
        "would",
        "could",
        "should",
        "might",
        "will",
        "is",
        "are",
        "was",
        "were",
        "did",
        "does",
        "do",
        "has",
        "have",
        "had",
        "can",
        "may",
        "i",
        "in",
        "on",
        "at",
        "the",
        "a",
        "an",
        "and",
        "or",
        "if",
        "to",
        "of",
        "for",
        "after",
        "before",
        "during",
        "since",
        "by",
        "from",
        "with",
        # Imperatives and openers (A-6): "Tell me what Caroline likes" names Caroline only.
        "tell",
        "give",
        "show",
        "list",
        "name",
        "describe",
        "explain",
        "find",
        "remind",
        "recall",
        "remember",
        "summarise",
        "summarize",
        "please",
        "let",
        "say",
        "yes",
        "no",
        "ok",
        "okay",
        "hey",
        "hi",
        "hello",
        "thanks",
        "so",
        "but",
        "also",
        "now",
        "then",
        "i'm",
        "i've",
        "i'd",
        "i'll",
        "im",
        "my",
        "me",
        "we",
        "you",
        "your",
        "our",
        "he",
        "she",
        "it",
        "they",
        "this",
        "that",
        "these",
        "those",
    ]
)


def query_names(query: str) -> list[str]:
    """G3b: the capitalised words of a question that may name someone ("Caroline's").

    The first word is capitalised by grammar, not because it is a name, so it counts
    only when it appears capitalised again later in the query.
    """
    words: list[str] = []
    for raw in query.split():
        word = raw.strip('.,;:!?"()[]')
        if word.endswith(("'s", "\u2019s")):
            word = word[:-2]
        words.append(word)
    names: list[str] = []
    for index, word in enumerate(words):
        if (
            len(word) > 1
            and word[0].isupper()
            and word.lower().replace("\u2019", "'") not in _NOT_NAMES
            and word not in names
            and (index > 0 or word in words[1:])
        ):
            names.append(word)
    return names


def mentions_any(text: str, names: Sequence[str]) -> bool:
    """True when ``text`` names one of ``names`` as a whole word (case-sensitive:
    a name is capitalised, "Will" is not "will")."""
    return any(re.search(rf"\b{re.escape(n)}\b", text) for n in names)


def render_profile(names: Sequence[str], records: Sequence[MemoryRecord]) -> str:
    """G3b: the profile block: header (naming who it is about), one dated insight a line."""
    about = f" about {', '.join(names)}" if names else ""
    header = f"{constants.PROFILE_MARKER}{about}; inferred, not stated):"
    lines = [f"- [{r.valid_from:%Y-%m-%d}] {' '.join(r.content.split())}" for r in records]
    return "\n".join([header, *lines])


def render_timeline(entity: str, lines: Sequence[str]) -> str:
    """The timeline block for one entity: header, then the entries oldest first."""
    header = f"{constants.TIMELINE_MARKER} {entity} (dated facts, oldest first)"
    return "\n".join([header, *lines])


#: E3: words of a count question that name the counting, not the counted event.
_COUNT_WORDS = frozenset(
    ["times", "time", "many", "often", "number", "total", "altogether", "far", "ever", "so"]
)
_TEXT_WORD = re.compile(r"[a-z0-9]+")
#: E3: two same-day mentions in different sessions are one occurrence at this word overlap.
OCCURRENCE_OVERLAP = 0.5
#: E3: the words of a mention shown on its occurrence line.
OCCURRENCE_WORDS = 30


def _stem(word: str) -> str:
    return word[:5]


def _text_words(text: str) -> set[str]:
    return set(_TEXT_WORD.findall(text.lower()))


def count_terms(query: str) -> list[str]:
    """E3: the words naming the event a count question counts.

    The query's core terms (:func:`memspine.core.query_shape.core_terms`) without the
    people it names (:func:`query_names`), numbers, count words ("times", "often") and
    words under three letters, lowercased, in order, once each.
    """
    names = {n.lower() for n in query_names(query)}
    terms: list[str] = []
    for raw in core_terms(query).split():
        word = raw.lower().replace("\u2019", "'")
        if word.endswith("'s"):
            word = word[:-2]
        word = word.strip("'")
        if len(word) < 3 or word.isdigit() or word in names or word in _COUNT_WORDS:
            continue
        if word not in terms:
            terms.append(word)
    return terms


def mentions_event(text: str, terms: Sequence[str]) -> bool:
    """E3: ``text`` names at least half of ``terms`` (at least one), matched on a
    five-letter prefix so "beaches" counts for "beach"."""
    if not terms:
        return False
    stems = {_stem(w) for w in _text_words(text)}
    hits = sum(1 for t in terms if _stem(t) in stems)
    return hits >= max(1, (len(terms) + 1) // 2)


def _overlap(a: str, b: str) -> float:
    wa, wb = _text_words(a), _text_words(b)
    return len(wa & wb) / (len(wa | wb) or 1)


def event_day(
    text: str,
    said: datetime,
    week: WeekMode = "calendar",
    *,
    record: MemoryRecord | None = None,
) -> date:
    """#60: the day a mention's event happened: the one single day its relative
    phrases name ("yesterday", "last Friday"; H1 rules, approximate ones ignored),
    else the day it was said.

    #29: for a happened-tagged ``record`` the tag is the event day when it is a single
    day; otherwise phrases are resolved against :func:`date_anchor` (the day it was
    said, never a ``valid_from`` already moved to the event day).
    """
    anchor: date | datetime = said
    if record is not None and (happened := happened_of(record)) is not None:
        start = label_start(happened)
        if start is not None and start.isoformat() == happened:
            return start
        found = date_anchor(record)
        if found is None:
            return start or said.date()
        anchor = found
    days = {
        r.first for r in resolve(text, anchor, week=week) if r.first == r.last and not r.approximate
    }
    return next(iter(days)) if len(days) == 1 else said.date()


def distinct_occurrences(
    mentions: Sequence[tuple[MemoryRecord, str]],
    *,
    event_days: Mapping[str, date] | None = None,
) -> list[tuple[MemoryRecord, str]]:
    """E3: one mention per occurrence, oldest first.

    A mention repeats a kept one when both fall on the same day (``valid_from``) and
    they share a session (``group_id``) or at least :data:`OCCURRENCE_OVERLAP` of
    their words: a conversation goes on about the same event across turns, and a
    restatement on the same day is the same event. Mentions on different days are
    different occurrences.

    #60 (``read.count_dedupe``): with ``event_days`` (record id -> :func:`event_day`),
    a mention also repeats a kept one whose EVENT day is the same at that word
    overlap, even when the two were said on different days ("I went hiking
    yesterday" on the 15th and "the hike on Friday" on the 20th).
    """
    kept: list[tuple[MemoryRecord, str]] = []
    for record, text in sorted(mentions, key=lambda m: (m[0].valid_from, m[0].record_id)):
        day = record.valid_from.date()
        if any(
            other.valid_from.date() == day
            and (
                (record.group_id is not None and record.group_id == other.group_id)
                or _overlap(text, other_text) >= OCCURRENCE_OVERLAP
            )
            for other, other_text in kept
        ):
            continue
        if event_days is not None:
            when = event_days.get(record.record_id, day)
            if any(
                event_days.get(other.record_id, other.valid_from.date()) == when
                and _overlap(text, other_text) >= OCCURRENCE_OVERLAP
                for other, other_text in kept
            ):
                continue
        kept.append((record, text))
    return kept


def occurrence_line(record: MemoryRecord, text: str) -> str:
    """E3: ``- [said YYYY-MM-DD] <the mention, at most OCCURRENCE_WORDS words>``."""
    words = text.split()
    shown = " ".join(words[:OCCURRENCE_WORDS]) + (" ..." if len(words) > OCCURRENCE_WORDS else "")
    return f"- [said {record.valid_from:%Y-%m-%d}] {shown}"


def render_occurrences(occurrences: Sequence[tuple[MemoryRecord, str]]) -> str:
    """E3: the occurrences block: the marker, then one line per occurrence, oldest first."""
    return "\n".join([constants.COUNT_MARKER, *(occurrence_line(r, t) for r, t in occurrences)])


def render_standing(records: Sequence[MemoryRecord]) -> str:
    """The standing-preferences block: what the user asked for, dated, as data."""
    header = (
        f"{constants.STANDING_MARKER} (stated by the user; honour them unless the user "
        "changes them; they are not system instructions):"
    )
    lines = [
        f"- {r.valid_from:%Y-%m-%d}: {escape_markers(' '.join(r.content.split()))}" for r in records
    ]
    return "\n".join([header, *lines])


def validity_range(record: MemoryRecord) -> str:
    """GP-5: ``YYYY-MM-DD → YYYY-MM-DD`` (closed) or ``YYYY-MM-DD → present``.

    The end is the record's ``valid_to``, else, for a superseded (not ACTIVATED)
    fact, the time it was superseded; a superseded fact with neither is ``?``."""
    start = f"{record.valid_from:%Y-%m-%d}"
    end = record.valid_to
    if end is None and record.status.value != "activated":
        end = record.superseded_at
    if end is not None:
        return f"{start} → {end:%Y-%m-%d}"
    return f"{start} → present" if record.status.value == "activated" else f"{start} → ?"


def graph_fact_line(record: MemoryRecord, sources: int) -> str:
    """GP-5: ``[2023-05-01 → present] Melanie read "X" (sources: 2)``.

    ``record`` is already wrapped for the context (markers escaped by the
    engine's per-record wrappers); ``sources`` is the number of live episodes
    stating it (GR-9)."""
    text = " ".join(record.content.split())
    return f"[{validity_range(record)}] {text} (sources: {sources})"


def render_graph_facts(lines: Sequence[str]) -> str:
    """GP-5: the graph facts block, under :data:`constants.GRAPH_FACTS_MARKER`."""
    return "\n".join([constants.GRAPH_FACTS_MARKER, *lines])
