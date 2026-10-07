"""W3 (plan v3.2, G02): the evidence-sufficiency signal of a read. Rules only, no model.

A read reports how strong its evidence is, so a caller (or a later read stage) can
tell a confident answer from a near miss:

- ``top_score`` and ``spread``: the best evidence score, and its lead over the mean of
  the next four (a flat ranking means no candidate stands out);
- ``distinct_days``: how many calendar days the top candidates come from (evidence
  for "all the times ..." questions spreads over time);
- ``answer_type`` / ``type_match``: the kind of answer the question asks for (a date,
  number, place or name) and whether any top candidate contains one at all;
- ``weak``: below ``read.evidence_weak_below``, or no top candidate of the asked type.

The persona (pinned context) is never evidence. English rules; a question without a
recognised answer type leaves ``type_match`` None, and only the score can make it weak.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass

from memspine.core.records import MemoryRecord

__all__ = [
    "EvidenceSignal",
    "answer_type",
    "contains_answer_type",
    "evidence_signal",
    "second_round_probe",
]

#: Candidates the signal looks at: the type check reads the top three, the spread
#: compares the best with the next four, the day count covers the top five.
_TYPE_TOP = 3
_SPREAD_TOP = 5

_QUESTION_TYPES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "date",
        re.compile(
            r"\b(?:when|what (?:date|day|month|year|time)|which (?:date|day|month|year)|"
            r"how long ago|since when|until when|"
            r"how many (?:days|weeks|months|years) (?:ago|before|after|since))\b",
            re.I,
        ),
    ),
    (
        "number",
        re.compile(
            r"\b(?:how (?:many|much|old|often|long|far)|what (?:number|percentage|amount))\b",
            re.I,
        ),
    ),
    (
        "place",
        re.compile(
            r"\b(?:where|which (?:city|country|state|town|place|restaurant|park|store)|"
            r"what (?:city|country|state|town|place))\b",
            re.I,
        ),
    ),
    (
        "name",
        re.compile(
            r"\b(?:who|whom|whose|what(?:'s| is| was) (?:the |his |her |their )?name)\b", re.I
        ),
    ),
)

_MONTHS = (
    r"jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|"
    r"sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?"
)
_DATE = re.compile(
    rf"\b(?:{_MONTHS})\b|\b(?:mon|tues|wednes|thurs|fri|satur|sun)day\b|\b(?:19|20)\d{{2}}\b|"
    r"\b\d{1,2}[/.-]\d{1,2}\b|\b(?:yesterday|today|tonight|tomorrow|ago|last|next|"
    r"weekend|week|month|year|morning|evening|summer|winter|spring|autumn|fall)\b",
    re.I,
)
_NUMBER = re.compile(
    r"\d|\b(?:one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|twenty|"
    r"thirty|forty|fifty|hundred|thousand|dozen|once|twice|thrice|half|couple)\b",
    re.I,
)
_PLACE_NOUN = re.compile(
    r"\b(?:city|town|village|country|state|park|beach|lake|mountain|river|home|house|"
    r"school|office|restaurant|cafe|store|shop|museum|church|hospital|airport|street|"
    r"island|camp|studio|gym|library|downtown|abroad)s?\b",
    re.I,
)
_PLACE_PREP = re.compile(r"\b(?:in|at|from|to|near|around)\s+(?:the\s+)?[A-Z][\w'-]+")
#: "Melanie: ..." — the speaker prefix of a stored turn is not an answer.
_SPEAKER = re.compile(r"^\s*[\w .'-]{1,40}:\s")
_CAPITALISED = re.compile(r"\b[A-Z][a-z][\w'-]*")
_NOT_NAMES = frozenset(
    [
        "I",
        "I'm",
        "I've",
        "I'll",
        "The",
        "A",
        "An",
        "And",
        "But",
        "So",
        "Then",
        "This",
        "That",
        "These",
        "Those",
        "It",
        "It's",
        "We",
        "You",
        "He",
        "She",
        "They",
        "My",
        "Our",
        "Your",
        "His",
        "Her",
        "Their",
        "Yes",
        "No",
        "Oh",
        "Hey",
        "Hi",
        "Thanks",
        "Wow",
        "Well",
        "Also",
        "Just",
        "What",
        "When",
        "Where",
        "Who",
        "Why",
        "How",
    ]
)


@dataclass(frozen=True)
class EvidenceSignal:
    """W3: how strong a read's evidence is (see the module docstring)."""

    top_score: float
    spread: float
    distinct_days: int
    candidates: int
    answer_type: str | None
    type_match: bool | None
    weak: bool


def answer_type(query: str) -> str | None:
    """The kind of answer ``query`` asks for: ``date``, ``number``, ``place``, ``name``,
    or None (first match in that order: "how many days ago" is a date question)."""
    for kind, pattern in _QUESTION_TYPES:
        if pattern.search(query):
            return kind
    return None


def contains_answer_type(text: str, kind: str) -> bool:
    """True when ``text`` holds at least one candidate answer of ``kind``."""
    body = _SPEAKER.sub("", text, count=1)
    if kind == "date":
        return bool(_DATE.search(body))
    if kind == "number":
        return bool(_NUMBER.search(body))
    if kind == "place":
        return bool(_PLACE_NOUN.search(body) or _PLACE_PREP.search(body))
    if kind == "name":
        return any(word not in _NOT_NAMES for word in _CAPITALISED.findall(body))
    return True


def evidence_signal(
    query: str,
    scored: Sequence[tuple[MemoryRecord, float]],
    weak_below: float | None = None,
) -> EvidenceSignal:
    """The W3 signal of ``scored`` (search order, best first) for ``query``."""
    evidence = sorted(
        ((record, score) for record, score in scored if record.source.channel != "persona"),
        key=lambda pair: -pair[1],
    )
    kind = answer_type(query)
    if not evidence:
        return EvidenceSignal(0.0, 0.0, 0, 0, kind, False if kind else None, True)
    top = evidence[0][1]
    rest = [score for _, score in evidence[1:_SPREAD_TOP]]
    spread = top - sum(rest) / len(rest) if rest else top
    days = {record.valid_from.date() for record, _ in evidence[:_SPREAD_TOP]}
    match = (
        any(contains_answer_type(record.content, kind) for record, _ in evidence[:_TYPE_TOP])
        if kind
        else None
    )
    weak = (weak_below is not None and top < weak_below) or match is False
    return EvidenceSignal(
        top_score=round(top, 6),
        spread=round(spread, 6),
        distinct_days=len(days),
        candidates=len(evidence),
        answer_type=kind,
        type_match=match,
        weak=weak,
    )


def second_round_probe(query: str, texts: Sequence[str], k: int = 6) -> str:
    """N04 (plan v3.2, EverOS / Honcho): the names and dates the first-round top hits
    mention and the question does not, as one probe text for a second search when the
    evidence is weak (multi-hop: the first hit names the bridge entity). Empty when
    nothing new is found."""
    asked = query.lower()
    found: list[str] = []
    for text in texts:
        body = _SPEAKER.sub("", text, count=1)
        for word in _CAPITALISED.findall(body):
            if word not in _NOT_NAMES and word.lower() not in asked and word not in found:
                found.append(word)
        for m in _DATE.finditer(body):
            token = m.group(0)
            if token[0].isdigit() and token.lower() not in asked and token not in found:
                found.append(token)
    return " ".join(found[:k])
