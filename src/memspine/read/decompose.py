"""R14-M-12 / OBS-10 (``read.decompose``): detect a multi-entity question, split it into one
sub-question per entity, and name the combine operator the reader should apply.

Pure rules, no model call. The map and reduce steps live in ``read/pipeline/decompose.py``.
This module only decides *whether* and *how* to split:

- an entity is a known participant (speaker or record entity) that the question names as a
  whole word, in order of appearance;
- the question is multi-entity when two or more entities are named and a join word links
  them (both / and / either / or / compare / same / each / common / versus / than / before /
  after) or the question has the "who ... more" form;
- each sub-question is the question with the other entities removed and the predicate kept;
- the combine operator is read from the surface words (intersection, union, compare, count,
  before-after).

``read.multi_intent_split`` is a different thing (discourse markers, one entity, several
requests); the two do not share code.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

#: Names shorter than this are never entities (initials, "al", "jo" read as words).
MIN_NAME_CHARS = 3

_JOIN = re.compile(
    r"\b(?:both|and|either|or|neither|compare[sd]?|comparison|same|each|common|together"
    r"|versus|vs|than|between|differ(?:ent|ence|s)?|similar(?:ity)?)\b",
    re.IGNORECASE,
)
_WHO_MORE = re.compile(
    r"\bwho\b.*\b(?:more|most|fewer|less|least|longer|earlier|later|older|younger|higher|lower)\b",
    re.IGNORECASE,
)
#: Join words dropped from a sub-question once the other entity is gone ("both", "either" ...).
_DANGLING = re.compile(r"\b(?:both|either|neither|each|together|also)\b\s*", re.IGNORECASE)

_INTERSECT = re.compile(
    r"\b(?:both|in common|common|same|shared?|all of them|each of them)\b", re.IGNORECASE
)
_UNION = re.compile(r"\b(?:either|combined|altogether|in total|together|all)\b", re.IGNORECASE)
_COMPARE = re.compile(
    r"\b(?:compare[sd]?|comparison|differ\w*|versus|vs|than|more|most|fewer|less|least|longer"
    r"|older|younger|higher|lower)\b",
    re.IGNORECASE,
)
_COUNT = re.compile(r"\bhow many\b|\bnumber of\b|\bcount\b|\btotal\b", re.IGNORECASE)
_BEFORE_AFTER = re.compile(
    r"\b(?:before|after|earlier|later|first|last|earliest|latest)\b", re.IGNORECASE
)

INTERSECTION = "intersection"
UNION = "union"
COMPARE = "compare"
BEFORE_AFTER = "before-after"
LIST = "list"

_HINT_TEXT = {
    INTERSECTION: (
        "keep only what the evidence supports under EVERY entity block (the items they share)"
    ),
    UNION: "combine what the evidence supports under any of the entity blocks",
    COMPARE: (
        "compare the entities' evidence side by side and answer which one the question asks for"
    ),
    BEFORE_AFTER: "put the dated items of the blocks on one timeline and answer from the order",
    LIST: "answer for each entity from its own block",
}


@dataclass(frozen=True)
class DecomposePlan:
    """The split of one multi-entity question. ``entities`` are in question order (display
    case as written); ``sub_queries[i]`` is the question about ``entities[i]`` alone."""

    entities: tuple[str, ...]
    sub_queries: tuple[str, ...]
    combine: str
    count: bool = False


_POSSESSIVE = "['" + chr(0x2019) + "]s"  # straight or typographic apostrophe


def _name_pattern(name: str) -> re.Pattern[str]:
    return re.compile(rf"(?<!\w){re.escape(name)}(?:{_POSSESSIVE})?(?!\w)", re.IGNORECASE)


def entities_in(query: str, known: Iterable[str], limit: int = 3) -> list[str]:
    """Known names the query mentions as whole words, in order of appearance (display case
    as written in the query, de-duplicated case-insensitively)."""
    found: list[tuple[int, str]] = []
    seen: set[str] = set()
    for raw in known:
        name = (raw or "").strip()
        if len(name) < MIN_NAME_CHARS or name.lower() in seen or name.lower() in _STOP_NAMES:
            continue
        m = _name_pattern(name).search(query)
        if m:
            seen.add(name.lower())
            written = query[m.start() : m.start() + len(name)]
            found.append((m.start(), written))
    found.sort()
    return [name for _, name in found][: max(2, limit)]


#: Speaker labels that are roles, not people.
_STOP_NAMES = frozenset({"user", "assistant", "system", "tool", "you", "me", "him", "her"})


def combine_operator(query: str) -> tuple[str, bool]:
    """The combine operator the question calls for and whether the result is a count."""
    count = bool(_COUNT.search(query))
    if _INTERSECT.search(query):
        return INTERSECTION, count
    if _BEFORE_AFTER.search(query) and not _COMPARE.search(query):
        return BEFORE_AFTER, count
    if _COMPARE.search(query) or _WHO_MORE.search(query):
        return COMPARE, count
    if _UNION.search(query):
        return UNION, count
    if _BEFORE_AFTER.search(query):
        return BEFORE_AFTER, count
    return LIST, count


def strip_entity(query: str, name: str) -> str:
    """``query`` without ``name``: a list separator next to it goes too ("A and B" -> "A")."""
    pat = _name_pattern(name).pattern
    out = re.sub(rf"\s*(?:,|and|or|&)\s*{pat}", "", query, flags=re.IGNORECASE)
    out = re.sub(rf"{pat}\s*(?:,|and|or|&)\s*", "", out, flags=re.IGNORECASE)
    out = re.sub(rf"\s*{pat}", "", out, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", out).strip()


def sub_query(query: str, entity: str, others: Iterable[str]) -> str:
    """The question about ``entity`` alone: the other entities removed, the predicate kept."""
    text = query
    for other in others:
        text = strip_entity(text, other)
    text = _DANGLING.sub("", text)
    return re.sub(r"\s{2,}", " ", text).strip(" ,") or query


def plan(query: str, known: Iterable[str], max_entities: int = 3) -> DecomposePlan | None:
    """The split for ``query``, or None when it is not a multi-entity question."""
    names = entities_in(query, known, limit=max_entities)
    if len(names) < 2:
        return None
    if not (_JOIN.search(query) or _WHO_MORE.search(query) or _BEFORE_AFTER.search(query)):
        return None
    names = names[:max_entities]
    subs = tuple(sub_query(query, n, [o for o in names if o != n]) for n in names)
    op, count = combine_operator(query)
    return DecomposePlan(tuple(names), subs, op, count)


def combine_hint(p: DecomposePlan) -> str:
    """The one-line hint that opens the merged context."""
    who = ", ".join(p.entities)
    line = f"[combine] Evidence is grouped per entity ({who}). To answer: {_HINT_TEXT[p.combine]}"
    if p.count:
        line += ", then give the count of the resulting items"
    return line + ". A turn relevant to several entities is listed once, under the first."


def entity_header(name: str, n_records: int) -> str:
    """The label that opens one entity's block."""
    return f"[entity: {name}] {n_records} evidence item(s) retrieved for this entity"
