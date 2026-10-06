"""#39 (SM-18): optional answer verification after the reader (``--verify-answer``).

:class:`VerifyingReader` wraps any reader. After the reader answers, one more model call
checks the answer against the same context with memspine's ``verify_answer`` prompt
(the prompt :meth:`memspine.Engine.verify_answer` uses, rendered and parsed the same
way). When the answer is not supported and the context supports another, the revised
answer replaces it; otherwise the reader's answer stands, and any verification failure
keeps it too. Off (the default), no wrapper is built, so readers, their prompts and
their ``describe()`` output are exactly as before.

The verifier calls the run's own chat backend (the judge's endpoint and model family),
so it is metered by the same budget. Each question then costs one more model call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from .contracts import ReaderAnswer
from .tokens import HeuristicTokenCounter

__all__ = ["VerifyingReader", "chat_verifier"]

#: ``async (question, answer, context) -> {"supported", "evidence_ids", "revised_answer"}``.
Verify = Callable[[str, str, str], Awaitable[Mapping[str, Any]]]


def chat_verifier(chat: Callable[..., Awaitable[str]]) -> tuple[Verify, str]:
    """A verifier over a bare ``async (prompt, system=None) -> str`` chat callable, and
    the ``prompt_version`` of the memspine ``verify_answer`` prompt it renders."""
    from memspine.core.answer import numbered_context, verification
    from memspine.prompts.models import AnswerVerdictOut
    from memspine.prompts.registry import PromptRegistry
    from memspine.services.llm.structured import structured_call

    prompt = PromptRegistry().get("verify_answer")

    class _ChatLLM:
        provider_id = "harness:verify"

        async def chat(self, messages: list[dict[str, str]], **_: Any) -> str:
            system = next((m["content"] for m in messages if m["role"] == "system"), None)
            user = "\n\n".join(m["content"] for m in messages if m["role"] != "system")
            return await chat(user, system=system)

    async def verify(question: str, answer: str, context: str) -> Mapping[str, Any]:
        lines, ids = numbered_context(context)
        verdict = await structured_call(
            _ChatLLM(),  # type: ignore[arg-type]
            prompt,
            {"question": question, "answer": answer, "context": "\n".join(lines)},
            AnswerVerdictOut,
        )
        return verification(
            verdict.supported, verdict.evidence, verdict.revised_answer, ids, answer
        )

    return verify, prompt.prompt_version


class VerifyingReader:
    """A reader whose answer is checked against its context (one more model call)."""

    def __init__(self, inner: Any, verify: Verify, prompt_version: str = "") -> None:
        self.inner = inner
        self._verify = verify
        self.prompt_version = prompt_version
        self.reader_id = f"{inner.reader_id}+verify"
        self.model = inner.model
        self.makes_model_calls = True
        self._counter = HeuristicTokenCounter()
        #: How many answers verification replaced, and how many verifications failed.
        self.revised = 0
        self.failures = 0

    def describe(self) -> Mapping[str, Any]:
        return {**self.inner.describe(), "verify_answer": self.prompt_version or True}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        try:
            verdict = await self._verify(question, first.text, context)
        except Exception:  # verification is an enhancer: the reader's answer stands
            self.failures += 1
            return _with_call(first, first.text, self._counter.count(context), 0)
        revised = verdict.get("revised_answer")
        text = first.text
        if not verdict.get("supported", True) and isinstance(revised, str) and revised.strip():
            self.revised += 1
            text = revised.strip()
        return _with_call(first, text, self._counter.count(context), self._counter.count(text))


def _with_call(
    first: ReaderAnswer, text: str, prompt_tokens: int, completion_tokens: int
) -> ReaderAnswer:
    """``first`` with ``text`` as the answer, plus the verification call (its token
    counts are estimates; a metered backend records the real ones)."""
    return ReaderAnswer(
        text=text,
        prompt_tokens=first.prompt_tokens + prompt_tokens,
        completion_tokens=first.completion_tokens + completion_tokens,
        latency_ms=first.latency_ms,
        model_calls=first.model_calls + 1,
        truncated=first.truncated,
        cached_prompt_tokens=first.cached_prompt_tokens,
        finish_reason=first.finish_reason,
        raw_text=first.raw_text,
    )
