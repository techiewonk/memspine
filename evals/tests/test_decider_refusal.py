"""I28: the refusal retry with a pluggable decider. Default behaviour is unchanged."""

from __future__ import annotations

import inspect
from typing import Any

from memspine_evals import refusal
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.refusal import RefusalRetryReader

from memspine.services.decision.decider import Decision

CONTEXT = "[2023-05-25] Caroline: I went hiking in Denver last Friday"


class _Reader:
    reader_id = "stub"
    model = "stub-model"
    makes_model_calls = True

    def __init__(self, *replies: str) -> None:
        self.replies = list(replies)
        self.questions: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        self.questions.append(question)
        return ReaderAnswer(text=self.replies.pop(0), prompt_tokens=10, model_calls=1)


class _Decider:
    decider_id = "fake"

    def __init__(self, label: str, confidence: float | None) -> None:
        self.label, self.confidence = label, confidence
        self.calls: list[tuple[str, str, str | None]] = []

    async def decide(self, task: str, question: str, context: str | None = None) -> Decision:
        self.calls.append((task, question, context))
        return Decision(self.label, self.confidence, None, task, self.decider_id)


async def test_default_has_no_decider_and_is_unchanged() -> None:
    reader = RefusalRetryReader(_Reader("Not mentioned in the conversation.", "Denver"))
    out = await reader.answer("Where did she hike?", CONTEXT)
    assert out.text == "Denver" and reader.retried == 1
    assert "decisions" not in out.extra_meta
    assert reader.reader_id == "stub+retry-neutral"
    assert "decider" not in reader.describe()


async def test_decider_overrides_the_regex_both_ways() -> None:
    # regex says "answer" (no refusal wording); the decider says it is a refusal -> retry
    dec = _Decider("refusal", 0.95)
    inner = _Reader("Unclear, sorry.", "Denver")
    reader = RefusalRetryReader(inner, decider=dec)
    dec_answers = iter(["refusal", "answer"])

    async def decide(task: str, question: str, context: str | None = None) -> Decision:
        dec.calls.append((task, question, context))
        return Decision(next(dec_answers), 0.95, None, task, "fake")

    dec.decide = decide  # type: ignore[method-assign]
    out = await reader.answer("Where did she hike?", CONTEXT)
    assert out.text == "Denver" and reader.retried == 1 and reader.recovered == 1
    assert [d["label"] for d in out.extra_meta["decisions"]] == ["refusal", "answer"]
    assert out.extra_meta["decisions"][0]["adapter"] == "fake"
    assert reader.reader_id.endswith("-fake")
    # regex says refusal; the decider is sure it is a real answer -> no retry
    dec2 = _Decider("answer", 0.9)
    # (I22: the default whole-answer match already says answer; legacy reproduces the regex hit)
    reader2 = RefusalRetryReader(
        _Reader("There is no doubt: Denver."), decider=dec2, refusal_match="legacy"
    )
    out2 = await reader2.answer("Where did she hike?", CONTEXT)
    assert reader2.retried == 0 and out2.text.startswith("There is no doubt")
    assert out2.extra_meta["decisions"][0]["heuristic"] is True


async def test_unsure_or_failing_decider_keeps_the_regex() -> None:
    reader = RefusalRetryReader(
        _Reader("I do not know.", "Denver"),
        decider=_Decider("answer", 0.55),
        decider_min_confidence=0.6,
    )
    out = await reader.answer("Where did she hike?", CONTEXT)
    assert reader.retried == 1 and out.text == "Denver"

    class Boom:
        decider_id = "boom"

        async def decide(self, *a: Any, **k: Any) -> Decision:
            raise RuntimeError("x")

    reader = RefusalRetryReader(_Reader("I do not know.", "Denver"), decider=Boom())
    out = await reader.answer("Where did she hike?", CONTEXT)
    assert reader.retried == 1
    assert out.extra_meta["decisions"][0]["error"] == "x"


async def test_decider_sees_question_and_answer_only_and_empty_skips_it() -> None:
    dec = _Decider("answer", 0.9)
    reader = RefusalRetryReader(_Reader("Denver"), decider=dec)
    await reader.answer("Where did she hike?", CONTEXT)
    assert dec.calls == [("refusal", "Where did she hike?", "Denver")]
    dec = _Decider("answer", 0.9)
    reader = RefusalRetryReader(_Reader("", "Denver"), decider=dec)
    out = await reader.answer("Where did she hike?", CONTEXT)
    assert reader.retried == 1 and dec.calls == [("refusal", "Where did she hike?", "Denver")]
    assert out.text == "Denver"


def test_no_gold_or_category_in_the_decider_path() -> None:
    src = inspect.getsource(RefusalRetryReader._refusal) + inspect.getsource(
        RefusalRetryReader.answer
    )
    for word in ("gold", "category", "abstention"):
        assert word not in src.lower().replace("the gold", "")
    assert refusal.is_refusal("I do not know")
