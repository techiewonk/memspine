"""W17d (plan v3.2, G21, ADR-060): a rule-based correction detector for user turns.

"No, I said Tuesday", "actually it's 7 pm", "not Monday, Tuesday", "that's wrong",
"correction: ...". A match needs a correction cue; it carries the replacement span
when the turn gives one and the negated span when it names the old value. Quoted or
reported speech ("she said 'no, ...'") and stock refusals ("no thanks") are skipped.
English only, no model. The engine resolves the target and acts on it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

__all__ = ["Correction", "detect_correction"]


@dataclass(frozen=True)
class Correction:
    """A detected correction: the cue that fired, the replacement value (None for a
    bare "that's wrong") and the negated old value when the turn names it."""

    cue: str
    replacement: str | None = None
    negated: str | None = None


_SPAN = r"[^.!?\n]{1,120}"
_FLAGS = re.IGNORECASE

#: Order matters: the most specific framings first.
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("correction", re.compile(rf"\bcorrection\s*:\s*(?P<rep>{_SPAN})", _FLAGS)),
    (
        "not_x_but_y",
        re.compile(
            rf"\bnot\s+(?P<neg>[^,.!?\n]{{1,60}}?)\s*(?:,\s*but|,|\bbut)\s+(?:it'?s\s+|it\s+is\s+)?"
            rf"(?P<rep>{_SPAN})",
            _FLAGS,
        ),
    ),
    (
        "no_i_said",
        re.compile(rf"^\s*(?:no|nope)\b[,.!]?\s*(?:i\s+said|i\s+meant)\s+(?P<rep>{_SPAN})", _FLAGS),
    ),
    (
        "actually",
        re.compile(
            rf"\bactually,?\s+(?:it'?s|it\s+is|it\s+was|its|that'?s|that\s+is)\s+(?P<rep>{_SPAN})",
            _FLAGS,
        ),
    ),
    ("i_said", re.compile(rf"\bi\s+(?:said|meant)\s+(?P<rep>{_SPAN})", _FLAGS)),
    (
        "no_it_is",
        re.compile(
            rf"^\s*(?:no|nope)\b[,.!]?\s*(?:it'?s|it\s+is|it\s+was|that'?s)\s+(?P<rep>{_SPAN})",
            _FLAGS,
        ),
    ),
    (
        "thats_wrong",
        re.compile(
            r"\b(?:that'?s|that\s+is|this\s+is)\s+(?:wrong|incorrect|not\s+right|not\s+correct)\b",
            _FLAGS,
        ),
    ),
)

#: Stock phrases that start with "no" but correct nothing.
_STOCK = re.compile(
    r"^\s*no\s+(?:thanks|thank\s+you|problem|worries|way|idea|need|rush|doubt)\b", _FLAGS
)
#: Reported speech: the correction belongs to someone else.
_REPORTED = re.compile(
    r"\b(?!(?:i|you)\b)\w+\s+(?:said|says|told\s+me|wrote)\s*[,:]?\s*[\"“']",
    _FLAGS,
)
_TRAILING_NOT = re.compile(r"^(?P<rep>.+?)\s*,?\s+not\s+(?P<neg>[^,.!?\n]{1,60})$", _FLAGS)


#: Straight and typographic quote marks trimmed off a span's ends.
_QUOTES = "\"'\u201c\u201d\u2018\u2019"


def _clean(span: str | None) -> str | None:
    if span is None:
        return None
    text = span.strip().strip(_QUOTES).strip(" ,;:")
    return text or None


def detect_correction(text: str) -> Correction | None:
    """The correction ``text`` (a user turn) makes, or None."""
    if not text.strip() or _STOCK.search(text) or _REPORTED.search(text):
        return None
    for cue, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is None:
            continue
        groups = match.groupdict()
        replacement = _clean(groups.get("rep"))
        negated = _clean(groups.get("neg"))
        if replacement and negated is None:
            # "I said Tuesday, not Monday": the old value trails the new one.
            trailing = _TRAILING_NOT.match(replacement)
            if trailing:
                replacement = _clean(trailing.group("rep"))
                negated = _clean(trailing.group("neg"))
        return Correction(cue=cue, replacement=replacement, negated=negated)
    return None
