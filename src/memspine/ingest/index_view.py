"""R14-INDEXVIEW (OBS-02/03/09/11, R14-M-4, R14-M-5): the index text of a turn record.

A turn's *display* text is what it said. Its *index* text is what the embedder and the lexical
index see, and it may carry words the turn leaves out: who spoke, the absolute date of the
session (with month and weekday words), a relative date resolved to a calendar date ("yesterday"
becomes the date), the content words of the previous turn when this one is elliptical ("yes, that
one"), and the participants' names behind a bare "we" / "they" / "you".

Everything here is deterministic and makes no model call. Dates come from the turn's OWN
timestamp, never the system clock; a turn with no timestamp gets no date tokens (a date is never
invented). The extra words are bounded by ``write.index_view_max_extra_tokens``. The record, its
id, its order and its display text are untouched: the index text rides in the WRITE event payload
(``index_text``), next to the record, and the vector and lexical projectors read it from there.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from datetime import date, datetime, timedelta

__all__ = [
    "PENDING_INDEX_PREFIX",
    "build_index_prefix",
    "compose_index_text",
    "elliptical_score",
    "plan_prefixes",
    "resolve_relative",
]

#: the extra words of the turn being written, handed to the write door (as ``PENDING_STAMP`` is)
PENDING_INDEX_PREFIX: ContextVar[str | None] = ContextVar("memspine_pending_index", default=None)

_MONTHS = (
    "January", "February", "March", "April", "May", "June", "July",
    "August", "September", "October", "November", "December",
)  # fmt: skip
_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}  # fmt: skip
_NUM = r"(\d{1,3}|a|an|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)"
_WORD = re.compile(r"[A-Za-z][A-Za-z'\-]*")

_STOP = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "been",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "he",
        "her",
        "hers",
        "him",
        "his",
        "how",
        "i",
        "if",
        "in",
        "is",
        "it",
        "its",
        "just",
        "me",
        "my",
        "no",
        "not",
        "of",
        "oh",
        "ok",
        "okay",
        "on",
        "or",
        "our",
        "out",
        "she",
        "so",
        "some",
        "than",
        "that",
        "the",
        "their",
        "them",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "to",
        "too",
        "up",
        "us",
        "was",
        "we",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
        "would",
        "yeah",
        "yes",
        "you",
        "your",
        "yours",
        "very",
        "really",
        "also",
        "got",
        "get",
        "going",
        "about",
        "like",
        "one",
        "more",
        "much",
        "many",
        "well",
        "sure",
        "thanks",
        "thank",
        "right",
        "yep",
        "nope",
        "hey",
        "hi",
        "hello",
    ]
)
_ELLIPTICAL_STARTS = frozenset(
    [
        "yes",
        "yeah",
        "yep",
        "sure",
        "exactly",
        "right",
        "absolutely",
        "definitely",
        "no",
        "nope",
        "it",
        "that",
        "this",
        "those",
        "these",
        "they",
        "he",
        "she",
        "them",
        "him",
        "her",
        "so",
        "and",
        "but",
        "or",
        "also",
        "same",
        "either",
        "neither",
        "both",
    ]
)
_PLURALS = {
    "we": "all", "us": "all", "our": "all", "ours": "all", "ourselves": "all",
    "they": "others", "them": "others", "their": "others",
    "you": "others", "your": "others", "y'all": "others",
}  # fmt: skip

_REL_SIMPLE = re.compile(
    r"\b(yesterday|today|tomorrow|tonight|last night|this morning|this evening|this afternoon)\b",
    re.IGNORECASE,
)
_REL_PERIOD = re.compile(r"\b(last|next|this) (week|weekend|month|year)\b", re.IGNORECASE)
_REL_WEEKDAY = re.compile(
    r"\b(last|next|this coming) (monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)
_REL_AGO = re.compile(
    rf"\b{_NUM} (day|week|month|year)s? (ago|from now)\b|\bin {_NUM} (day|week|month|year)s?\b",
    re.IGNORECASE,
)


def _fmt(d: date) -> str:
    return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"


def _add_months(d: date, months: int) -> date:
    index = d.year * 12 + (d.month - 1) + months
    year, month = divmod(index, 12)
    return date(year, month + 1, 1)


def _count(token: str) -> int | None:
    token = token.lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _shift(anchor: date, unit: str, n: int) -> str:
    unit = unit.lower()
    if unit == "day":
        return _fmt(anchor + timedelta(days=n))
    if unit == "week":
        return _fmt(anchor + timedelta(weeks=n))
    if unit == "month":
        first = _add_months(anchor.replace(day=1), n)
        return f"{_MONTHS[first.month - 1]} {first.year}"
    return str(anchor.year + n)


def resolve_relative(text: str, anchor: date) -> list[str]:
    """Calendar phrases for the relative-date expressions in ``text``, resolved against ``anchor``
    (the turn's own date). Order of appearance, de-duplicated; the spoken word is kept so a query
    for either form matches ("yesterday 7 May 2023")."""
    found: list[tuple[int, str]] = []
    for m in _REL_SIMPLE.finditer(text):
        word = m.group(1).lower()
        if word == "yesterday" or word == "last night":
            out = _fmt(anchor - timedelta(days=1))
        elif word == "tomorrow":
            out = _fmt(anchor + timedelta(days=1))
        else:  # today, tonight, this morning/evening/afternoon
            out = _fmt(anchor)
        found.append((m.start(), f"{word} {out}"))
    for m in _REL_PERIOD.finditer(text):
        which, unit = m.group(1).lower(), m.group(2).lower()
        sign = {"last": -1, "next": 1, "this": 0}[which]
        if unit == "week":
            monday = anchor - timedelta(days=anchor.weekday()) + timedelta(weeks=sign)
            out = f"week of {_fmt(monday)}"
        elif unit == "weekend":
            saturday = anchor - timedelta(days=anchor.weekday()) + timedelta(days=5, weeks=sign)
            out = f"weekend of {_fmt(saturday)}"
        else:
            out = _shift(anchor, unit, sign)
        found.append((m.start(), f"{which} {unit} {out}"))
    for m in _REL_WEEKDAY.finditer(text):
        which, name = m.group(1).lower(), m.group(2).lower()
        target = [d.lower() for d in _WEEKDAYS].index(name)
        delta = (anchor.weekday() - target) % 7
        if which == "last":
            day = anchor - timedelta(days=delta or 7)
        else:
            day = anchor + timedelta(days=(target - anchor.weekday()) % 7 or 7)
        found.append((m.start(), f"{which} {name} {_fmt(day)}"))
    for m in _REL_AGO.finditer(text):
        if m.group(1) is not None:
            n, unit, sign = _count(m.group(1)), m.group(2), -1 if m.group(3).lower() == "ago" else 1
        else:
            n, unit, sign = _count(m.group(4)), m.group(5), 1
        if n is None or n > 400:
            continue
        found.append((m.start(), f"{m.group(0).lower()} {_shift(anchor, unit, sign * n)}"))
    found.sort(key=lambda pair: pair[0])
    return list(dict.fromkeys(phrase for _, phrase in found))


def _words(text: str) -> list[str]:
    return _WORD.findall(text)


def _content_words(text: str) -> list[str]:
    return [w for w in _words(text) if w.lower() not in _STOP and len(w) > 2]


def elliptical_score(text: str, *, max_words: int = 8) -> int:
    """Rule score 0-3 of how incomplete a turn is on its own: short (at most ``max_words`` words),
    starts with a pronoun or an assent word, carries no content noun (fewer than two content
    words and no capitalised name after the first word)."""
    words = _words(text)
    if not words:
        return 0
    score = 0
    if len(words) <= max_words:
        score += 1
    if words[0].lower() in _ELLIPTICAL_STARTS:
        score += 1
    names = [w for w in words[1:] if w[:1].isupper()]
    if len(_content_words(text)) < 2 and not names:
        score += 1
    return score


def _unique(words: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for w in words:
        key = w.lower()
        if key not in seen:
            seen.add(key)
            out.append(w)
    return out


def build_index_prefix(
    *,
    speaker: str,
    stamp: datetime | None,
    content: str,
    prev_content: str | None,
    prev_speaker: str | None,
    participants: Sequence[str],
    max_extra_tokens: int = 48,
    elliptical_threshold: int = 2,
    elliptical_max_words: int = 8,
) -> str:
    """The words prepended to a turn's content to form its index text ("" when there is nothing to
    add). ``stamp`` is the turn's own event time (None: no date tokens). The result is cut to
    ``max_extra_tokens`` whitespace tokens, dropping the previous-turn words first."""
    head: list[str] = []
    if speaker:
        head.append(f"{speaker}:")
    anchor = stamp.date() if stamp is not None else None
    if anchor is not None:
        head.append(f"{_fmt(anchor)} {_MONTHS[anchor.month - 1]} {_WEEKDAYS[anchor.weekday()]}")
        head.extend(resolve_relative(content, anchor))
    lowered = {w.lower() for w in _words(content)}
    plural = {_PLURALS[w] for w in lowered if w in _PLURALS}
    if plural and participants:
        names = [
            p
            for p in participants
            if p and ("all" in plural or p.strip().lower() != speaker.strip().lower())
        ]
        if names:
            head.append("with " + " ".join(_unique(names)))
    tail: list[str] = []
    if (
        prev_content
        and elliptical_score(content, max_words=elliptical_max_words) >= elliptical_threshold
    ):
        words = _unique(_content_words(prev_content))
        if prev_speaker and prev_speaker.strip().lower() != speaker.strip().lower():
            words = [prev_speaker, *words]
        tail = ["re:", *words]
    tokens = " ".join(head).split()
    room = max(0, max_extra_tokens - len(tokens))
    tokens = tokens[:max_extra_tokens]
    tokens += " ".join(tail).split()[:room]
    return " ".join(tokens)


def compose_index_text(prefix: str, content: str) -> str | None:
    """The index text for ``content`` under ``prefix`` (None when the prefix adds nothing, so the
    event carries no ``index_text`` and the projectors use the content as before)."""
    return f"{prefix}\n{content}" if prefix else None


def plan_prefixes(
    turns: Sequence[Mapping[str, object]],
    *,
    speaker_of: Callable[[Mapping[str, object]], str],
    stamp_of: Callable[[Mapping[str, object]], datetime | None],
    content_of: Callable[[Mapping[str, object]], str],
    max_extra_tokens: int,
    elliptical_threshold: int,
    elliptical_max_words: int,
) -> list[str]:
    """One prefix per turn of a call, aligned with ``turns``. The previous turn is the previous
    one in the call (any role), so a reply sees what it answers."""
    names = _unique([s for t in turns if (s := speaker_of(t))])
    out: list[str] = []
    prev_content: str | None = None
    prev_speaker: str | None = None
    for turn in turns:
        try:
            content = content_of(turn)
        except (KeyError, TypeError):
            out.append("")
            continue
        speaker = speaker_of(turn)
        out.append(
            build_index_prefix(
                speaker=speaker,
                stamp=stamp_of(turn),
                content=content,
                prev_content=prev_content,
                prev_speaker=prev_speaker,
                participants=names,
                max_extra_tokens=max_extra_tokens,
                elliptical_threshold=elliptical_threshold,
                elliptical_max_words=elliptical_max_words,
            )
        )
        prev_content, prev_speaker = content, speaker
    return out
