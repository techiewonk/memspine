"""E06: an answer-slot verifier, a reader post-step (``--verify-slots``).

After the reader answers, code checks the answer against the question's typed contract
(``memspine.core.query_contract`` / ``memspine.core.answer_check``): does a "when" answer hold a
date, a "how many" answer a number, a "which city" answer a city and not a country, a yes/no
answer a polarity, a count equal the items listed, and (when the reader kept a list or the
caller has an evidence table) every listed item appear in the answer. Code decides; the model
is not asked whether the answer is fine, so the verifier cannot be correlated with the reader.

Only an answer with a named defect gets ONE bounded repair call: the inner reader is asked the
same question again with the previous answer and the specific defect appended, over the same
context (the same memories, no new evidence). The repair is kept only when it is not an
abstention and the code check no longer finds the defect; otherwise the first answer stands.
An answer that already abstains ("not mentioned", "cannot be determined") is never repaired:
an evidence-based unknown must not become a guess.

Reader-agnostic: it needs only ``question``, ``context`` and the inner reader's ``answer``;
nothing is read from the gold, the dataset or the category (rule I37). Off (the default) no
wrapper is built, so rows are byte-identical. Cost: one extra reader call per flagged answer,
recorded in the row's ``meta["slot_verify"]`` and added to the answer's token counts and
``model_calls``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import replace
from typing import Any

from .contracts import ReaderAnswer

__all__ = [
    "SLOT_VERIFY_MODES",
    "SLOT_VERIFY_VERSION",
    "SlotVerifyReader",
    "repair_question",
]

SLOT_VERIFY_VERSION = "v1"
#: ``strict`` repairs only the strict defects; ``soft`` also acts on the noisier ``soft_*``
#: signals (an unnamed place / person / title).
SLOT_VERIFY_MODES = ("strict", "soft")

REPAIR_TEMPLATE = (
    "{question}\n\n"
    "A previous answer to this question was: {previous}\n"
    "It has this problem: {defects}\n"
    "{type_line}"
    "Answer the question again from the same memories. Fix only that problem and keep what "
    "was right. If the memories do not contain what is asked, reply exactly: Not mentioned in "
    "the conversation."
)


def repair_question(question: str, previous: str, defects_text: str, type_line: str) -> str:
    """The question text of the repair call (the inner reader wraps it in its own prompt)."""
    return REPAIR_TEMPLATE.format(
        question=question.strip(),
        previous=previous.strip()[:600],
        defects=defects_text.strip(),
        type_line=(type_line + "\n") if type_line else "",
    )


def _is_abstention(text: str) -> bool:
    from memspine.core.answer_check import is_abstention

    from .refusal import is_refusal

    return is_refusal(text) or is_abstention(text)


class SlotVerifyReader:
    """Wraps a reader (see the module docstring)."""

    def __init__(
        self,
        inner: Any,
        mode: str = "strict",
        *,
        contract_fn: Callable[[str], Any] | None = None,
    ) -> None:
        if mode not in SLOT_VERIFY_MODES:
            raise ValueError(f"slot-verify mode must be one of {SLOT_VERIFY_MODES}, got {mode!r}")
        if contract_fn is None:
            from memspine.core.query_contract import build_contract

            contract_fn = build_contract
        self.inner = inner
        self.mode = mode
        self._contract = contract_fn
        self.guard = getattr(inner, "guard", None)
        self.reader_id = f"{inner.reader_id}+slots-{mode}"
        self.model = inner.model
        self.makes_model_calls = True
        #: counters for the run log
        self.seen = 0
        self.flagged = 0
        self.repaired = 0
        self.rejected = 0
        self.extra_calls = 0

    def describe(self) -> Mapping[str, Any]:
        return {
            **self.inner.describe(),
            "slot_verify": self.mode,
            "slot_verify_version": SLOT_VERIFY_VERSION,
        }

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        first: ReaderAnswer = await self.inner.answer(question, context, question_date)
        self.seen += 1
        meta: dict[str, Any] = {"version": SLOT_VERIFY_VERSION, "mode": self.mode}
        try:
            result = await self._verify(question, context, question_date, first, meta)
        except Exception as exc:  # an enhancer, never a gate: the reader's answer stands
            meta.update({"outcome": "error", "error": str(exc)[:200]})
            result = first
        return replace(result, extra_meta={**dict(result.extra_meta), "slot_verify": meta})

    async def _verify(
        self,
        question: str,
        context: str,
        question_date: str | None,
        first: ReaderAnswer,
        meta: dict[str, Any],
    ) -> ReaderAnswer:
        from memspine.core.answer_check import check_answer, defect_instruction

        contract = self._contract(question)
        meta["contract"] = contract.as_meta()
        if not contract.known:
            meta["outcome"] = "skipped_unknown_type"
            return first
        if not first.text.strip() or _is_abstention(first.text):
            meta["outcome"] = "skipped_abstention"  # an unknown stays unknown
            return first
        soft = self.mode == "soft"
        defects = check_answer(
            contract, first.text, explanation=first.raw_text or "", abstained=False, soft=soft
        )
        if not defects:
            meta["outcome"] = "clean"
            return first
        self.flagged += 1
        meta["defects"] = [d.as_meta() for d in defects]
        meta["previous_answer"] = first.text
        type_line = f"The answer should be a {contract.type_label.replace(':', ' ')}."
        asked = repair_question(question, first.text, defect_instruction(defects), type_line)
        fixed: ReaderAnswer = await self.inner.answer(asked, context, question_date)
        self.extra_calls += max(fixed.model_calls, 1)
        meta["repair"] = {
            "answer": fixed.text,
            "model_calls": fixed.model_calls,
            "prompt_tokens": fixed.prompt_tokens,
            "completion_tokens": fixed.completion_tokens,
            "latency_ms": fixed.latency_ms,
        }
        kept = first
        if not fixed.text.strip():
            meta["outcome"] = "rejected_empty"
        elif _is_abstention(fixed.text):
            meta["outcome"] = "rejected_abstention"
        else:
            again = {
                d.kind
                for d in check_answer(
                    contract,
                    fixed.text,
                    explanation=fixed.raw_text or "",
                    abstained=False,
                    soft=soft,
                )
            }
            if again & {d.kind for d in defects}:
                meta["outcome"] = "rejected_still_defective"
            else:
                meta["outcome"] = "repaired"
                kept = fixed
        if kept is first:
            self.rejected += 1
        else:
            self.repaired += 1
        return ReaderAnswer(
            text=kept.text,
            prompt_tokens=first.prompt_tokens + fixed.prompt_tokens,
            completion_tokens=first.completion_tokens + fixed.completion_tokens,
            latency_ms=first.latency_ms + fixed.latency_ms,
            model_calls=first.model_calls + fixed.model_calls,
            truncated=kept.truncated,
            cached_prompt_tokens=first.cached_prompt_tokens + fixed.cached_prompt_tokens,
            finish_reason=kept.finish_reason,
            raw_text=kept.raw_text,
            prompt_variant=first.prompt_variant,
            extra_meta=dict(first.extra_meta),
        )
