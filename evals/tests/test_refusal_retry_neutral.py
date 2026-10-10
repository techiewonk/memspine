"""I1: the refusal retry is neutral by default and never uses the gold or the category."""

from __future__ import annotations

import inspect
from typing import Any

import pytest
from memspine_evals import refusal
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.refusal import (
    RETRY_INSTRUCTION,
    RETRY_INSTRUCTION_ASSERTIVE,
    RETRY_INSTRUCTION_NEUTRAL,
    RefusalRetryReader,
    shares_content_word,
)

CONTEXT = "[2023-05-25] Caroline: I went hiking in Denver last Friday"
CLAIMS = (
    "do contain",
    "does contain",
    "relevant to this question",
    "best-supported",
    "do not reply",
)


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
        return ReaderAnswer(
            text=self.replies.pop(0), prompt_tokens=10, completion_tokens=2, model_calls=1
        )


def test_neutral_wording_claims_no_evidence_and_is_the_default() -> None:
    text = RETRY_INSTRUCTION_NEUTRAL.lower()
    for claim in CLAIMS:
        assert claim not in text, claim
    assert "not mentioned in the conversation" in text
    assert "if they truly do not" in text
    assert RETRY_INSTRUCTION == RETRY_INSTRUCTION_NEUTRAL


def test_assertive_wording_is_still_available_unchanged() -> None:
    assert "do contain information relevant to this question" in RETRY_INSTRUCTION_ASSERTIVE
    assert "best-supported evidence" in RETRY_INSTRUCTION_ASSERTIVE


async def test_default_mode_is_neutral_and_recorded_in_meta() -> None:
    inner = _Reader("Not mentioned in the conversation", "Not mentioned in the conversation")
    reader = RefusalRetryReader(inner)
    out = await reader.answer("Where did she go?", CONTEXT)
    assert inner.questions[1] == "Where did she go?" + RETRY_INSTRUCTION_NEUTRAL
    assert out.text == "Not mentioned in the conversation"  # a correct refusal survives
    assert out.extra_meta["retry_mode"] == "neutral"
    assert out.extra_meta["retry_accepted"] is False
    assert reader.describe()["retry_mode"] == "neutral"
    assert reader.reader_id == "stub+retry-neutral"


async def test_assertive_mode_uses_old_wording_and_historical_id() -> None:
    inner = _Reader("not mentioned", "Denver")
    reader = RefusalRetryReader(inner, mode="assertive")
    out = await reader.answer("Where?", CONTEXT)
    assert inner.questions[1] == "Where?" + RETRY_INSTRUCTION_ASSERTIVE
    assert out.extra_meta["retry_mode"] == "assertive" and out.text == "Denver"
    assert reader.reader_id == "stub+retry"


def test_unknown_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        RefusalRetryReader(_Reader(), mode="pushy")


def test_context_overlap_helper() -> None:
    assert shares_content_word("Denver", CONTEXT)
    assert not shares_content_word("Paris", CONTEXT)
    assert not shares_content_word("the conversation", CONTEXT)  # stopwords only


async def test_overlap_valve_is_off_by_default_and_rejects_ungrounded_when_on() -> None:
    off = await RefusalRetryReader(_Reader("Not mentioned", "Paris")).answer("Where?", CONTEXT)
    assert off.text == "Paris" and off.extra_meta["retry_accepted"] is True
    on = RefusalRetryReader(_Reader("Not mentioned", "Paris"), require_context_overlap=True)
    out = await on.answer("Where?", CONTEXT)
    assert out.text == "Not mentioned" and out.extra_meta["retry_accepted"] is False
    assert out.extra_meta["retry_require_context_overlap"] is True


def test_no_gold_or_category_in_the_retry_path() -> None:
    src = inspect.getsource(refusal.RefusalRetryReader.answer)
    for leak in ("gold", "category", "abstention", "meta"):
        assert leak not in src.replace("extra_meta", ""), leak
    params = inspect.signature(refusal.RefusalRetryReader.answer).parameters
    assert list(params) == ["self", "question", "context", "question_date"]
