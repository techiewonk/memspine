"""Query shape detection for the routed read (H3). Rules only, English, no model.

Aggregation questions ("how many times ...", "what books has X read?", "list ...") need
evidence spread over several sessions; a single top-k ranking returns the few best-matching
turns and misses the rest. On LoCoMo, 28.6% of multi-hop questions had only part of their
evidence retrieved. :func:`is_aggregation` routes them to the ``compose`` read.
"""

from __future__ import annotations

import re

__all__ = [
    "content_words",
    "core_terms",
    "feedback_terms",
    "is_aggregation",
    "is_count",
    "is_inference",
    "is_novelty",
    "is_ordering",
    "is_personal",
    "is_temporal",
    "is_verbatim",
    "rule_read_mode",
    "split_intents",
]

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


_TEMPORAL = re.compile(
    r"\b(?:when|what (?:date|day|month|year|time)|which (?:date|day|month|year)|"
    r"how long (?:ago|before|after|since)|how many (?:days|weeks|months|years)|"
    r"since when|until when)\b",
    re.I,
)


def is_temporal(query: str) -> bool:
    """True when the question asks for a date or a time span ("when did ...")."""
    return bool(_TEMPORAL.search(query))


#: A modal or "likely" ahead of the question mark: the answer is inferred, not stated.
_INFERENCE = re.compile(r"\b(?:would|likely|might|could)\b[^?]*\?", re.I)


def is_inference(query: str) -> bool:
    """True when the question asks what someone would, might or could do, or is likely
    to ("Would Caroline pursue writing?", "Is it likely that ...?"): a modal word, or
    "likely", before the question mark. Pure and self-contained (C1)."""
    return bool(_INFERENCE.search(query))


#: F5 (plan v3.2): the asker wants someone's own words, not a paraphrase: "what did
#: Gina say about ...", "how did Jon describe ...", "what were her exact words", "quote
#: ...". Up to six words for the speaker ("the posters at the poetry reading"). A
#: bare "mention" is an attribute lookup ("which movie does Tim mention"), so only
#: "mention about" counts; a bare "quote" is a noun too often ("a motivational quote").
_VERBATIM = re.compile(
    r"\b(?:what|how) (?:exactly )?(?:did|does|do|has|have|had|was|were) (?:[\w'-]+ ){1,6}"
    r"(?:say|said|tell|told|write|wrote|describe|described|put it|phrase|word|"
    r"mention(?:ed)? about)\b"
    r"|\b(?:exact|own|actual) words\b|\bword for word\b|\bverbatim\b",
    re.I,
)


def is_verbatim(query: str) -> bool:
    """True when the question asks for what someone said, in their words ("What does
    Gina say about the dancers?", "How did Jon describe the studio?", "her exact
    words"). Such a question is answered from the raw turn, never from a summary."""
    return bool(_VERBATIM.search(query))


#: Units after "how many" that ask for a duration ("how many days ago ..."), not a count.
_DURATION_UNITS = r"(?:seconds|minutes|hours|days|weeks|weekends|months|years|decades)"
_COUNT = re.compile(rf"\bhow (?:many|often)\b(?! {_DURATION_UNITS}\b)", re.I)


def is_count(query: str) -> bool:
    """True when the question asks how many times something happened or how many there
    are ("how many times ...", "how many pets ...", "how often ..."); a duration ("how
    many days ago ...") is a date question, not a count."""
    return bool(_COUNT.search(query))


def is_aggregation(query: str) -> bool:
    """True when the question asks for a count, a list or several items."""
    return bool(_AGGREGATE.search(query))


def rule_read_mode(query: str) -> str | None:
    """G24: the read mode the rules settle before a decision provider is asked.

    ``compose`` for a count, or for a list / set question that is not a date or duration
    question; ``replay`` for an ordering question (evidence shown in time order); None
    when the rules leave the choice open.
    """
    if is_count(query) or (is_aggregation(query) and not is_temporal(query)):
        return "compose"
    if is_ordering(query):
        return "replay"
    return None


def core_terms(query: str) -> str:
    """The query without interrogative and function words: a second, lexical-leaning probe."""
    words = [w for w in _WORD.findall(query) if w.lower() not in _STOP]
    return " ".join(words)


_CONTENT_WORD = re.compile(r"[a-z0-9]+")


def content_words(text: str) -> frozenset[str]:
    """The lowercase content words of ``text``: alphanumeric runs of two or more
    characters that are not function words ("Alice's party" -> {alice, party}).
    Used for lexical overlap without a model (#61 cue matching, #62 coverage)."""
    return frozenset(
        w for w in _CONTENT_WORD.findall(text.lower()) if len(w) > 1 and w not in _STOP
    )


def feedback_terms(query: str, texts: list[str], k: int = 5, min_docs: int = 2) -> str:
    """N03 (plan v3.2, pseudo-relevance feedback, Mnemon ``feedback``): the content
    words that at least ``min_docs`` of ``texts`` (the first-round top hits) share and
    the query lacks, most shared first, up to ``k`` of them, as one probe text. Words
    of three letters or fewer and speaker-like capitalised prefixes are skipped.
    Empty when nothing qualifies."""
    asked = content_words(query)
    counts: dict[str, int] = {}
    for text in texts:
        body = text.split(": ", 1)[1] if ": " in text[:40] else text
        for word in content_words(body):
            if len(word) > 3 and word not in asked:
                counts[word] = counts.get(word, 0) + 1
    shared = sorted((w for w, n in counts.items() if n >= min_docs), key=lambda w: (-counts[w], w))
    return " ".join(shared[:k])


#: W9 (plan v3.2): a question about the user, the conversation partner or a choice they
#: face ("what should I cook", "recommend a book for me", "my", "you").
_PERSONAL = re.compile(
    r"\b(?:i|i'm|i've|i'd|i'll|me|my|mine|myself|we|us|our|ours|you|your|yours)\b"
    r"|\b(?:recommend|suggest|should|advice|advise|plan|help|choose|pick|prefer|favou?rite|"
    r"for tonight|for dinner|for the weekend)\b",
    re.IGNORECASE,
)


def is_personal(query: str) -> bool:
    """W9: True when the question is about the asker or a choice they face. A
    general-knowledge question ("What is the capital of France?") is not, and gets no
    profile or preference block under ``read.profile_scope_gate``."""
    return bool(_PERSONAL.search(query))


#: G34 (plan v3.2): discourse markers that join two requests in one question.
_INTENT_SPLIT = re.compile(
    r"\s*(?:[;?]\s+|,?\s+(?:and also|and additionally|additionally|also,|plus,?|"
    r"as well as|what I really want(?: to know)? is|and then)\s+"
    # "..., and where did he move?": split before the question word, keeping it.
    r"|,?\s+and\s+(?=(?:what|where|when|who|whom|which|how|why)\b))",
    re.IGNORECASE,
)


def split_intents(query: str, min_words: int = 3) -> list[str]:
    """G34: the separate requests in a multi-part question ("What did Jon say about
    the studio, and where did he move?" -> two parts). A part shorter than
    ``min_words`` words stays with the previous one; a single request comes back as
    ``[query]``."""
    parts: list[str] = []
    for piece in _INTENT_SPLIT.split(query):
        piece = piece.strip(" ,.?")
        if not piece:
            continue
        if parts and len(piece.split()) < min_words:
            parts[-1] = f"{parts[-1]} {piece}"
        else:
            parts.append(piece)
    return parts or [query]


#: G32 (plan v3.2): the asker wants something NEW ("a book I haven't read", "something
#: different from last time", "other than sushi").
_NOVELTY = re.compile(
    r"\b(?:something|anything|somewhere|someone) (?:new|different|else)\b"
    r"|\b(?:haven't|have not|never) (?:tried|read|seen|watched|been|visited|done|heard)\b"
    r"|\bother than\b|\bnot already\b|\bdifferent from (?:last time|before|usual)\b"
    r"|\bnew (?:ideas|places|books|recipes|things|restaurants|shows|hobbies)\b",
    re.IGNORECASE,
)


def is_novelty(query: str) -> bool:
    """G32: True when the question asks for something the asker has not had yet."""
    return bool(_NOVELTY.search(query))
