"""G25 (plan v3.2, W14): a user's "forget that" said in dialogue. Rules only.

"Please forget what I said about my ex", "don't remember my address", "stop using
my old job title", "delete that from your memory". :func:`is_forget_request` finds
the request; :func:`forget_target` strips the request words, leaving the text that
names what to forget, which a search turns into candidate records. Nothing is
deleted on a regex: the engine tags the turn and lists candidates
(:meth:`memspine.Engine.forget_requests`); the caller confirms and calls ``forget``.
"""

from __future__ import annotations

import re

__all__ = ["forget_target", "is_forget_request"]

#: An imperative position: the start of a sentence or clause, or after a polite cue.
_IMPERATIVE = r"(?:\b(?:please|can you|could you|would you|kindly) |^\s*|[.!?,;]\s+)"
_REQUEST = re.compile(
    # Imperative only: "I always forget my keys", "I don't remember where it is"
    # describe the speaker, they ask nothing.
    _IMPERATIVE + r"(?:"
    r"forget (?:that|this|it|about|what I (?:said|told you)|my\b)"
    r"|(?:don't|do not|never) (?:remember|store|keep|save|record)\b"
    r"|stop (?:remembering|storing|keeping|saving|recording|using|bringing up|mentioning)\b"
    r"|(?:delete|erase|remove|wipe) (?:that|this|it|my\b|what I said)"
    r")",
    re.IGNORECASE | re.MULTILINE,
)
_CUE_WORDS = re.compile(
    r"\b(?:please|can you|could you|forget|don't|do not|stop|never|remember|remembering|"
    r"store|storing|keep|keeping|save|saving|record|recording|delete|erase|remove|wipe|"
    r"from (?:your |the )?memory|memory of|what I (?:said|told you)|about|that|this|it|"
    r"using|bringing up|mentioning)\b",
    re.IGNORECASE,
)


def is_forget_request(text: str) -> bool:
    """True when ``text`` asks the assistant to forget or stop using something."""
    return bool(_REQUEST.search(text))


def forget_target(text: str) -> str:
    """``text`` without the request words: what to forget ("my old address")."""
    return " ".join(_CUE_WORDS.sub(" ", text).split()).strip(" .,!?:;")
