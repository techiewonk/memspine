"""Refusal detection and the opt-in refusal-retry reader (``--retry-refusal``).

Gap analysis of a 9B reader on LoCoMo (``analysis/READER_GAPS.md``): about half of the wrong
answers with the gold evidence in context were refusals ("not mentioned"). With
``--retry-refusal`` a refusal is re-asked once with a firmer instruction to answer from the
best available evidence. Off (the default) no wrapper is built and readers are unchanged.

The patterns copy ``failure_buckets.REFUSAL`` / ``DENIAL`` (that module lives outside the
package); a test pins the two to the same source text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .contracts import ReaderAnswer
from .tokens import HeuristicTokenCounter

__all__ = ["DENIAL", "REFUSAL", "RETRY_INSTRUCTION", "RefusalRetryReader", "is_refusal"]

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

#: Appended to the question on the retry. The memories are said to contain relevant lines,
#: the reader is told to commit to the best-supported answer and to compute dates.
RETRY_INSTRUCTION = (
    "\n\nNote: the memories above do contain information relevant to this question, so do not "
    "reply that it is unknown or not mentioned. Answer from the best-supported evidence in "
    "them, even if it is indirect: resolve relative times against the date of the line they "
    "appear in, and give your most likely answer in a few words."
)


def is_refusal(answer: str) -> bool:
    """True when ``answer`` is empty, declines ("I do not know", "not mentioned", ...) or
    opens with a denial of the premise."""
    text = answer.strip()
    if not text:
        return True
    return bool(REFUSAL.search(text) or DENIAL.search(text))


class RefusalRetryReader:
    """Wraps a reader: a refusal on a non-empty context is re-asked once, firmer.

    The retry costs one more reader call (counted in ``model_calls``, so it counts against
    ``--max-model-calls``). The retry's answer replaces the first only when it is not
    itself a refusal; both answers are kept in ``ReaderAnswer.extra_meta`` (row ``meta``).
    """

    def __init__(self, inner: Any, instruction: str = RETRY_INSTRUCTION) -> None:
        self.inner = inner
        self.guard = getattr(inner, "guard", None)
        self.instruction = instruction
        self.reader_id = f"{inner.reader_id}+retry"
        self.model = inner.model
        self.makes_model_calls = True
        self._counter = HeuristicTokenCounter()
        #: Refusals seen, retries that produced a non-refusal answer.
        self.retried = 0
        self.recovered = 0

    def describe(self) -> Mapping[str, Any]:
        return {**self.inner.describe(), "retry_refusal": True}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        if not context.strip() or not is_refusal(first.text):
            return first
        self.retried += 1
        second: ReaderAnswer = await self.inner.answer(
            question + self.instruction, context, question_date
        )
        accepted = not is_refusal(second.text)
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
                # D3 [HAR-2]: row prompt_tokens sums both calls; keep the split
                "first_prompt_tokens": first.prompt_tokens,
                "first_completion_tokens": first.completion_tokens,
                "retry_prompt_tokens": second.prompt_tokens,
                "retry_completion_tokens": second.completion_tokens,
            },
        )
