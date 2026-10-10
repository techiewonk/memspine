"""I32: a "no record" signal for memory-level sycophancy (opt-in ``--no-record-hint``).

A user may assert a past event that never happened ("do you remember when I told you about my
trip to Lisbon?"). A reader shown unrelated memories tends to play along. When the question
asserts a past event and nothing retrieved supports it, this wrapper tells the reader that no
matching memory was found, so it can say so.

Both decisions are small swappable functions (the OpenDecider agent may replace the detector
with a model ``noul`` question, "is this event supported by the memories?"):

* :func:`asserts_past_event` - first-person past-event recall cues in the question text.
* :func:`event_supported` - the share of the question's content words found in the context.

Neither uses gold labels, categories or benchmark wording. Off (the default) builds no wrapper.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable, Mapping
from typing import Any

from .contracts import ReaderAnswer

__all__ = [
    "NO_RECORD_NOTE",
    "SUPPORT_MIN_FRACTION",
    "NoRecordHintReader",
    "asserts_past_event",
    "event_supported",
    "strip_public_knowledge",
]

#: Told to the reader (prepended to the context) when the asserted event has no support.
NO_RECORD_NOTE = (
    "[Note: no stored memory matches the past event the question describes. Do not confirm "
    "it; say it is not in the memories.]"
)

_CUES = re.compile(
    r"\bdo you (?:still )?(?:remember|recall)\b"
    r"|\bremember (?:when|that|how|the time)\b"
    r"|\b(?:as )?i (?:told|mentioned|said|asked|showed|sent|shared) (?:you|to you)\b"
    r"|\bwhen i (?:told|mentioned|said|asked|showed|sent|shared|went|visited|got|bought|met|had)\b"
    r"|\bthe (?:time|day|night) (?:i|we)\b"
    r"|\blast time (?:i|we) (?:talked|spoke|discussed|met)\b"
    r"|\bwe (?:talked|spoke|discussed) about\b"
    r"|\byou (?:said|told me|promised|recommended|suggested)\b",
    re.IGNORECASE,
)
_WORD = re.compile(r"[a-z0-9]{4,}")
_STOP = frozenset(
    (
        "that this with from have has had were was been being they them their there then than "
        "what when where which while would could should about into over also does did remember "
        "recall told mentioned said asked showed sent shared when time know think still "
        "talked spoke discussed promised recommended suggested please tell"
    ).split()
)

#: Fraction of the question's content words that must occur in the context for the asserted
#: event to count as supported: a majority, a fixed principled cut (not tuned to any dataset).
SUPPORT_MIN_FRACTION = 0.5


_PUBLIC_HEAD = re.compile(r"^(?:\[[^\]]*\]\s*)?\[public knowledge\]", re.IGNORECASE)


def strip_public_knowledge(context: str) -> str:
    """E03: the context without its ``[public knowledge]`` blocks (the marker line and the
    ``- `` lines under it). General background never supports a past event of a person."""
    kept: list[str] = []
    inside = False
    for line in context.split("\n"):
        if _PUBLIC_HEAD.match(line):
            inside = True
            continue
        if inside and line.startswith("- "):
            continue
        inside = False
        kept.append(line)
    return "\n".join(kept)


def asserts_past_event(question: str) -> bool:
    """True when the question asserts or recalls a past event with a first-person cue."""
    return bool(_CUES.search(question))


def event_supported(question: str, context: str) -> bool:
    """True when at least :data:`SUPPORT_MIN_FRACTION` of the question's content words occur
    in the context. A question with no content words has nothing to check and counts as
    supported (no note is added)."""
    words = {w for w in _WORD.findall(question.lower()) if w not in _STOP}
    if not words:
        return True
    ctx = set(_WORD.findall(context.lower()))
    return len(words & ctx) / len(words) >= SUPPORT_MIN_FRACTION


class NoRecordHintReader:
    """Wraps a reader: adds :data:`NO_RECORD_NOTE` to the context of an asserted-event
    question with no supporting memory. Same number of reader calls as the inner reader."""

    def __init__(
        self,
        inner: Any,
        *,
        detector: Callable[[str], bool] = asserts_past_event,
        support: Callable[[str, str], bool] = event_supported,
    ) -> None:
        self.inner = inner
        self.guard = getattr(inner, "guard", None)
        self.detector = detector
        self.support = support
        self.reader_id = f"{inner.reader_id}+norecord"
        self.model = inner.model
        self.makes_model_calls = getattr(inner, "makes_model_calls", True)
        #: Questions seen that asserted a past event, and of those how many got the note.
        self.asserted = 0
        self.flagged = 0

    def describe(self) -> Mapping[str, Any]:
        return {**self.inner.describe(), "no_record_hint": True}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        flagged = False
        if self.detector(question):
            self.asserted += 1
            if not self.support(question, strip_public_knowledge(context)):
                flagged = True
                self.flagged += 1
                context = f"{NO_RECORD_NOTE}\n{context}" if context.strip() else NO_RECORD_NOTE
        result: ReaderAnswer = await self.inner.answer(question, context, question_date)
        if flagged:
            meta = {**(result.extra_meta or {}), "no_record_hint": True}
            result = dataclasses.replace(result, extra_meta=meta)
        return result
