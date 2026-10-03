"""Query shape detection for the routed read (H3). Rules only, English, no model.

Aggregation questions ("how many times ...", "what books has X read?", "list ...") need
evidence spread over several sessions; a single top-k ranking returns the few best-matching
turns and misses the rest. On LoCoMo, 28.6% of multi-hop questions had only part of their
evidence retrieved. :func:`is_aggregation` routes them to the ``compose`` read.
"""

from __future__ import annotations

import re

__all__ = ["core_terms", "is_aggregation", "is_ordering"]

#: Time words after which "every" / "each" describe a habit ("every morning"), not a set.
_HABIT = (
    r"(?:other|day|days|morning|evening|night|week|weekend|month|year|summer|winter|spring|"
    r"autumn|fall|monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b"
)
_AGGREGATE = re.compile(
    r"\bhow (?:many|often)\b"
    # "how much" asks for one amount unless the question sums over occasions.
    r"|\bhow much\b.*\b(?:in total|total|altogether|in all|combined)\b"
    r"|\blist\b|\ball (?:the|of)\b"
    rf"|\bevery\b(?! {_HABIT})|\beach\b(?! {_HABIT})"
    r"|\bwhat (?:are|were) (?:\w+'s |the |some |all )?\w+s\b"
    # A plural set noun counts only as the thing asked for ("what kinds", "which
    # events"), not inside a single-item question ("which of the books ...").
    r"|\b(?:what|which) (?:\w+ )?"
    r"(?:names|kinds|types|ways|activities|places|books|events|hobbies|items|things)\b"
    r"|\bwhat \w+s (?:has|have|did|does|do)\b",
    re.I,
)
_STOP = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "to",
        "in",
        "on",
        "at",
        "for",
        "by",
        "with",
        "from",
        "and",
        "or",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "has",
        "have",
        "had",
        "do",
        "does",
        "did",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "many",
        "much",
        "often",
        "list",
        "all",
        "every",
        "each",
        "some",
        "any",
        "that",
        "this",
        "these",
        "those",
        "it",
        "its",
        "they",
        "them",
        "their",
        "he",
        "she",
        "his",
        "her",
        "i",
        "you",
        "we",
        "us",
        "our",
        "my",
        "me",
        "your",
    ]
)
_WORD = re.compile(r"[A-Za-z0-9']+")


_ORDERING = re.compile(
    r"\b(?:first|last time|latest|most recent(?:ly)?|earliest|recently|in what order|"
    r"before or after|which came first|start(?:ed)? (?:to|doing)|since when)\b"
    # "the last book", "Melanie's last trip"; not "last week" (a relative date).
    r"|(?:\b(?:the|her|his|their|my|your|our)|'s) last\b(?! (?:few |couple of )?"
    r"(?:week|weekend|month|year|night|summer|winter|spring|autumn|fall|monday|tuesday|"
    r"wednesday|thursday|friday|saturday|sunday)s?\b)",
    re.I,
)


def is_ordering(query: str) -> bool:
    """True when the answer depends on temporal order (H16: show evidence in time order)."""
    return bool(_ORDERING.search(query))


def is_aggregation(query: str) -> bool:
    """True when the question asks for a count, a list or several items."""
    return bool(_AGGREGATE.search(query))


def core_terms(query: str) -> str:
    """The query without interrogative and function words: a second, lexical-leaning probe."""
    words = [w for w in _WORD.findall(query) if w.lower() not in _STOP]
    return " ".join(words)
