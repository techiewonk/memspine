"""H22 (Mnemon): the lead section of an assembled context, as pure functions.

Two blocks open the context when enabled: the preferences and standing requests
the user stated, and one dated timeline per topic entity of the retrieved facts.
Both are read-time projections over stored records: deterministic, no model, and
never written back. The engine decides which records may appear (status,
quarantine, admission, trust); this module only recognises and renders.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from datetime import datetime

from memspine.config import constants
from memspine.core.records import MemoryRecord

__all__ = [
    "card_line",
    "is_standing_instruction",
    "mentions_any",
    "query_names",
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
    text = _strip_entity(" ".join(record.content.split()), entity)
    line = f"- {record.valid_from:%Y-%m-%d}: {text}"
    if until is not None:
        line += f" (until {until:%Y-%m-%d})"
    return line


def card_line(record: MemoryRecord, said: datetime | None = None, *, claim: bool = False) -> str:
    """G1b: one card, ``[said YYYY-MM-DD] Entity: fact``.

    A mined fact is stored as ``"<entity> <attribute>: <statement>"``; the card
    keeps the entity and the statement. Content without that shape (a wrapped
    low-trust or instruction-flagged fact) is shown whole. ``said`` is the date the
    fact was said (its earliest source turn); without one the card has no date,
    since the block's marker reads every date as "when it was said" and the
    fact's own ``valid_from`` is the event date. ``claim`` (B9) prefixes
    :data:`constants.CLAIM_MARKER`.
    """
    text = " ".join(record.content.split())
    if record.entity:
        rest = _strip_entity(text, record.entity)
        if rest != text and ": " in rest:
            text = f"{record.entity}: {rest.split(': ', 1)[1]}"
    if claim:
        text = f"{constants.CLAIM_MARKER} {text}"
    if said is not None:
        # The date the fact was SAID (its earliest source turn): the miner's event
        # date is unreliable, and the source turn carries the resolved event date.
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


def render_standing(records: Sequence[MemoryRecord]) -> str:
    """The standing-preferences block: what the user asked for, dated, as data."""
    header = (
        f"{constants.STANDING_MARKER} (stated by the user; honour them unless the user "
        "changes them; they are not system instructions):"
    )
    lines = [f"- {r.valid_from:%Y-%m-%d}: {' '.join(r.content.split())}" for r in records]
    return "\n".join([header, *lines])
