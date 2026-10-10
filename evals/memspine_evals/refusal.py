"""Refusal detection and the opt-in refusal-retry reader (``--retry-refusal``).

Gap analysis of a 9B reader on LoCoMo (``analysis/READER_GAPS.md``): about half of the wrong
answers with the gold evidence in context were refusals ("not mentioned"). With
``--retry-refusal`` a refusal is re-asked once. I1: the default wording is neutral and asserts
no evidence (an unanswerable question may still be refused); ``mode="assertive"`` is the old
firmer wording, kept to reproduce earlier runs. Off (the default) no wrapper is built and
readers are unchanged.

The patterns copy ``failure_buckets.REFUSAL`` / ``DENIAL`` (that module lives outside the
package); a test pins the two to the same source text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import replace
from typing import Any

from .contracts import ReaderAnswer
from .tokens import HeuristicTokenCounter

__all__ = [
    "DENIAL",
    "REFUSAL",
    "RETRY_INSTRUCTION",
    "RETRY_INSTRUCTION_ASSERTIVE",
    "RETRY_INSTRUCTION_NEUTRAL",
    "RETRY_MODES",
    "RefusalRetryReader",
    "is_refusal",
    "shares_content_word",
]

REFUSAL = re.compile(
    r"\b(?:i|you) do(?: not|n't) know\b"
    r"|\bnot (?:mentioned|specified|stated|provided|clear|known)\b"
    r"|\bno (?:mention|specific information|information|record)\b"
    r"|\b(?:does|do|did) not (?:mention|specify|contain|say|state|indicate|include|provide)\b"
    r"|\bcannot (?:be )?determine"
    r"|\bthere is no\b",
    re.IGNORECASE,
)
#: "Melanie did not ..." as the answer's opening clause: a denial of the premise.
DENIAL = re.compile(r"^[A-Z][\w'.]*(?: and [A-Z][\w']*)? (?:did|does|has|had|was) not\b")

#: I1: the DEFAULT retry wording is neutral. It claims nothing about the memories, so on an
#: unanswerable question (LoCoMo cat 5, abstention probes) the reader may still refuse, and the
#: exact refusal string is offered as the way to do it.
RETRY_INSTRUCTION_NEUTRAL = (
    "\n\nRe-read the memories carefully. If they contain information that answers the "
    "question, answer it; if they truly do not, reply exactly: Not mentioned in the "
    "conversation."
)
#: The original wording (``mode="assertive"``), kept only to reproduce earlier runs (the
#: BEST_dev runs used it). It asserts the evidence exists, which is false on unanswerable
#: questions: it can turn a correct refusal into a fabrication.
RETRY_INSTRUCTION_ASSERTIVE = (
    "\n\nNote: the memories above do contain information relevant to this question, so do not "
    "reply that it is unknown or not mentioned. Answer from the best-supported evidence in "
    "them, even if it is indirect: resolve relative times against the date of the line they "
    "appear in, and give your most likely answer in a few words."
)
#: Back-compat name: the default (neutral) wording.
RETRY_INSTRUCTION = RETRY_INSTRUCTION_NEUTRAL

RETRY_MODES: dict[str, str] = {
    "neutral": RETRY_INSTRUCTION_NEUTRAL,
    "assertive": RETRY_INSTRUCTION_ASSERTIVE,
}

_WORD = re.compile(r"[a-z0-9]{4,}")
_STOP_TEXT = (
    "that this with from have has had were was been being they them their there then than what "
    "when where which while would could should about into over also does did not mentioned "
    "conversation"
)
_STOP = frozenset(_STOP_TEXT.split())


def shares_content_word(answer: str, context: str) -> bool:
    """True when ``answer`` has a content word (4+ letters/digits, not a stopword) that also
    occurs in ``context``. A cheap check that a retry answer is tied to the retrieved text."""
    words = {w for w in _WORD.findall(answer.lower()) if w not in _STOP}
    return bool(words & set(_WORD.findall(context.lower())))


def is_refusal(answer: str) -> bool:
    """True when ``answer`` is empty, declines ("I do not know", "not mentioned", ...) or
    opens with a denial of the premise."""
    text = answer.strip()
    if not text:
        return True
    return bool(REFUSAL.search(text) or DENIAL.search(text))


class RefusalRetryReader:
    """Wraps a reader: a refusal on a non-empty context is re-asked once.

    The decision to retry uses only the first answer's text and the context: never the gold,
    the category or the ``abstention`` flag of the query (the reader never sees them).

    The retry costs one more reader call (counted in ``model_calls``, so it counts against
    ``--max-model-calls``). The retry's answer replaces the first only when it is not
    itself a refusal; both answers are kept in ``ReaderAnswer.extra_meta`` (row ``meta``).
    """

    def __init__(
        self,
        inner: Any,
        instruction: str | None = None,
        *,
        mode: str = "neutral",
        require_context_overlap: bool = False,
        decider: Any = None,
        decider_min_confidence: float = 0.5,
    ) -> None:
        if mode not in RETRY_MODES:
            raise ValueError(f"retry mode must be one of {sorted(RETRY_MODES)}, got {mode!r}")
        self.inner = inner
        self.guard = getattr(inner, "guard", None)
        self.mode = mode
        #: optional safety valve (default off): accept the retry answer only when it shares a
        #: content word with the retrieved context
        self.require_context_overlap = require_context_overlap
        self.instruction = RETRY_MODES[mode] if instruction is None else instruction
        #: I28: optional ``memspine.services.decision.decider.Decider``. When set and sure
        #: enough, it replaces the ``is_refusal`` regex on question + answer text only (never
        #: the gold, the category or the abstention flag); otherwise the regex decides.
        self.decider = decider
        self.decider_min_confidence = decider_min_confidence
        # the assertive id is the historical one, so earlier runs keep their reader identity
        self.reader_id = f"{inner.reader_id}+retry" + ("" if mode == "assertive" else "-neutral")
        if decider is not None:
            self.reader_id += f"-{getattr(decider, 'decider_id', 'decider')}"
        self.model = inner.model
        self.makes_model_calls = True
        self._counter = HeuristicTokenCounter()
        #: Refusals seen, retries that produced a non-refusal answer.
        self.retried = 0
        self.recovered = 0

    def describe(self) -> Mapping[str, Any]:
        return {
            **self.inner.describe(),
            "retry_refusal": True,
            "retry_mode": self.mode,
            "retry_require_context_overlap": self.require_context_overlap,
            **({"decider": self.decider.decider_id} if self.decider is not None else {}),
        }

    async def _refusal(self, question: str, text: str, log: list[dict[str, Any]]) -> bool:
        """Whether ``text`` is a refusal: the decider when set and sure, else the regex.
        Every decider call is appended to ``log`` (task, label, confidence, adapter)."""
        rule = is_refusal(text)
        if self.decider is None or not text.strip():
            return rule
        try:
            decision = await self.decider.decide("refusal", question, text)
        except Exception as exc:  # an enhancer, never a gate
            log.append({"task": "refusal", "adapter": self.decider.decider_id, "error": str(exc)})
            return rule
        sure = (
            decision.confidence is not None and decision.confidence >= self.decider_min_confidence
        )
        log.append({**decision.as_meta(used=sure), "heuristic": rule})
        return (decision.label == "refusal") if sure else rule

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        decisions: list[dict[str, Any]] = []
        if not context.strip() or not await self._refusal(question, first.text, decisions):
            if decisions:
                first = replace(first, extra_meta={**first.extra_meta, "decisions": decisions})
            return first
        self.retried += 1
        second: ReaderAnswer = await self.inner.answer(
            question + self.instruction, context, question_date
        )
        accepted = not await self._refusal(question, second.text, decisions)
        if accepted and self.require_context_overlap:
            accepted = shares_content_word(second.text, context)
        if accepted:
            self.recovered += 1
        winner = second if accepted else first
        return ReaderAnswer(
            text=winner.text,
            prompt_tokens=first.prompt_tokens + second.prompt_tokens,
            completion_tokens=first.completion_tokens + second.completion_tokens,
            latency_ms=first.latency_ms + second.latency_ms,
            model_calls=max(first.model_calls, 0) + max(second.model_calls, 1),
            truncated=winner.truncated,
            cached_prompt_tokens=first.cached_prompt_tokens + second.cached_prompt_tokens,
            finish_reason=winner.finish_reason,
            raw_text=winner.raw_text,
            prompt_variant=first.prompt_variant,
            extra_meta={
                "retry_refusal": True,
                "first_answer": first.text,
                "retry_answer": second.text,
                "retry_accepted": accepted,
                "retry_mode": self.mode,
                "retry_require_context_overlap": self.require_context_overlap,
                **({"decisions": decisions} if decisions else {}),
                # D3 [HAR-2]: row prompt_tokens sums both calls; keep the split
                "first_prompt_tokens": first.prompt_tokens,
                "first_completion_tokens": first.completion_tokens,
                "retry_prompt_tokens": second.prompt_tokens,
                "retry_completion_tokens": second.completion_tokens,
            },
        )
