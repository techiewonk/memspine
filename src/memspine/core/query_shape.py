"""Query shape detection for the routed read (H3). Rules only, English, no model.

Aggregation questions ("how many times ...", "what books has X read?", "list ...") need
evidence spread over several sessions; a single top-k ranking returns the few best-matching
turns and misses the rest. On LoCoMo, 28.6% of multi-hop questions had only part of their
evidence retrieved. :func:`is_aggregation` routes them to the ``compose`` read.
"""

from __future__ import annotations

import re

__all__ = ["core_terms", "is_aggregation"]

_AGGREGATE = re.compile(
    r"\bhow (?:many|often|much)\b"
    r"|\blist\b|\ball (?:the|of)\b|\bevery\b|\beach\b"
    r"|\bwhat (?:are|were) (?:\w+'s |the |some |all )?\w+s\b"
    r"|\b(?:names|kinds|types|ways|activities|places|books|events|hobbies|items|things)\b"
    r"|\bwhat \w+s (?:has|have|did)\b",
    re.I,
)
_STOP = frozenset(
    ["a", "an", "the", "of", "to", "in", "on", "at", "for", "by", "with", "from", "and", "or", "is", "are", "was", "were", "be", "been", "has", "have", "had", "do", "does", "did", "what", "which", "who", "whom", "whose", "when", "where", "why", "how", "many", "much", "often", "list", "all", "every", "each", "some", "any", "that", "this", "these", "those", "it", "its", "they", "them", "their", "he", "she", "his", "her", "i", "you", "we", "us", "our", "my", "me", "your"]
)
_WORD = re.compile(r"[A-Za-z0-9']+")


def is_aggregation(query: str) -> bool:
    """True when the question asks for a count, a list or several items."""
    return bool(_AGGREGATE.search(query))


def core_terms(query: str) -> str:
    """The query without interrogative and function words: a second, lexical-leaning probe."""
    words = [w for w in _WORD.findall(query) if w.lower() not in _STOP]
    return " ".join(words)
