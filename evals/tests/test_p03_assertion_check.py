"""P03 assertion check: supported / contradicted / unknown, no model call (a fake decider)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from memspine_evals.assertion_check import (
    AssertionCheckReader,
    Verdict,
    asserts_claim,
    chat_decider,
    classify_claim,
    detect_claims,
    memory_lines,
)
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.no_record import asserts_past_event

CTX = (
    "[2023-03-01] Maya: I adopted a grey cat named Pixel in March.\n"
    "[2023-03-09] Maya: I ran 10 kilometres on Sunday along the river.\n"
    "[2023-04-02] Maya: I never drink coffee, tea only.\n"
    "[public knowledge]\n- Lisbon is the capital of Portugal.\n"
)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def kinds(msg: str) -> list[str]:
    return [c.kind for c in detect_claims(msg)]


def test_detects_past_recall_and_habit_claims() -> None:
    assert kinds("I went to Lisbon last summer and loved it.") == ["past"]
    assert kinds("Do you remember when I told you about my sister's wedding?") == ["recall"]
    assert kinds("As I told you, the deadline is Friday.") == ["recall"]
    assert kinds("I always run on Sundays.") == ["habit"]
    assert kinds("We bought a house in 2019, so suggest a garden plan.") == ["past"]
    assert kinds("You said the vet was open on Mondays.") == ["recall"]


def test_preferences_and_requests_stated_now_are_not_claims() -> None:
    for msg in (
        "I love spicy food, what should I cook?",
        "I prefer window seats.",
        "I always prefer tea over coffee, so suggest a tea.",
        "I want a vegetarian menu.",
        "Can you recommend a book for the train?",
        "What do you think about remote work?",
    ):
        assert not detect_claims(msg), msg
    # a value next to a real past claim: only the claim sentence is checked
    msg = "I love Lisbon. I visited it in 2019 with my sister."
    assert [c.text for c in detect_claims(msg)] == ["I visited it in 2019 with my sister."]


def test_broader_than_the_i32_detector() -> None:
    probes = [
        "I ran a marathon in Berlin, what recovery plan do you suggest?",
        "We moved to Austin two years ago, any neighbourhood tips?",
        "Remember when I told you about Pixel? Is she settled in?",
    ]
    new = sum(asserts_claim(p) for p in probes)
    old = sum(asserts_past_event(p) for p in probes)
    assert new == 3 and old < new


def test_classify_supported_contradicted_unknown() -> None:
    lines = memory_lines(CTX)
    assert classify_claim("I adopted a grey cat named Pixel in March", lines).label == "supported"
    # the memory has the same event with another number
    v = classify_claim("I ran 15 kilometres on Sunday along the river", lines)
    assert v.label == "contradicted" and "10 kilometres" in v.line
    # polarity flip on an anchored line
    v = classify_claim("I drink coffee every morning, tea only", lines)
    assert v.label in ("contradicted", "unknown")
    # nothing in memory: unknown, never contradicted
    assert classify_claim("I flew a helicopter over Alaska", lines).label == "unknown"
    # public knowledge never supports a personal event
    assert classify_claim("I visited the capital of Portugal", lines).label == "unknown"
    assert classify_claim("anything at all", []).label == "unknown"


def test_a_missing_specific_without_an_alternative_is_unknown_not_contradicted() -> None:
    lines = ["I adopted a grey cat in March"]
    v = classify_claim("I adopted a grey cat named Pixel in March", lines)
    assert v.label == "unknown"  # the name is not in memory, but nothing conflicts


class Fake:
    reader_id = "fake"
    model = "m"
    makes_model_calls = True

    def __init__(self) -> None:
        self.contexts: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": "fake"}

    async def answer(self, question: str, context: str, question_date: str | None = None):
        self.contexts.append(context)
        return ReaderAnswer(
            text="ok", prompt_tokens=5, completion_tokens=1, latency_ms=1.0, model_calls=1
        )


def test_reader_notes_by_verdict_and_leaves_other_messages_untouched() -> None:
    inner = Fake()
    reader = AssertionCheckReader(inner)
    run(reader.answer("I adopted a grey cat named Pixel in March, how is she?", CTX))
    assert "supports" in inner.contexts[-1] and inner.contexts[-1].endswith(CTX)
    run(reader.answer("I ran 15 kilometres on Sunday along the river, good pace?", CTX))
    assert (
        "differs from a stored memory" in inner.contexts[-1]
        and "10 kilometres" in inner.contexts[-1]
    )
    ans = run(reader.answer("Remember when I flew a helicopter over Alaska?", CTX))
    note = inner.contexts[-1]
    assert "no record" in note and "do not say it did not happen" in note
    assert ans.extra_meta["assertion_check"]["verdict"] == "unknown"
    assert ans.model_calls == 1
    run(reader.answer("I love jazz, suggest an album.", CTX))
    assert inner.contexts[-1] == CTX  # a preference is taken at face value: no note
    assert reader.detected == 3 and reader.verdicts == {
        "supported": 1,
        "contradicted": 1,
        "unknown": 1,
    }
    assert reader.describe()["assertion_check"] == "v1/rules"


def test_worst_verdict_decides_and_empty_context_still_gets_the_note() -> None:
    inner = Fake()
    reader = AssertionCheckReader(inner)
    run(reader.answer("I adopted a grey cat named Pixel in March. I flew to Mars once.", CTX))
    assert "no record" in inner.contexts[-1]
    run(reader.answer("Remember when I flew to Mars?", ""))
    assert inner.contexts[-1].startswith("[Note: no stored memory confirms")


def test_llm_decider_is_used_when_valid_and_the_rules_otherwise() -> None:
    async def chat_ok(prompt: str, system: str | None = None) -> str:
        return 'Sure {"verdict": "contradicted", "line": "L2"}'

    async def chat_bad_line(prompt: str, system: str | None = None) -> str:
        return '{"verdict": "supported", "line": "L99"}'

    inner = Fake()
    reader = AssertionCheckReader(inner, "llm", decider=chat_decider(chat_ok))
    ans = run(reader.answer("I adopted a grey cat named Pixel in March, how is she?", CTX))
    claim = ans.extra_meta["assertion_check"]["claims"][0]
    assert claim["label"] == "contradicted" and claim["method"] == "llm"
    assert "ran 10 kilometres" in claim["line"]  # the line text comes from the context
    fallback = AssertionCheckReader(Fake(), "llm", decider=chat_decider(chat_bad_line))
    ans = run(fallback.answer("I adopted a grey cat named Pixel in March, how is she?", CTX))
    assert ans.extra_meta["assertion_check"]["claims"][0]["method"] == "rules"
    with pytest.raises(ValueError):
        AssertionCheckReader(Fake(), "llm")
    with pytest.raises(ValueError):
        AssertionCheckReader(Fake(), "bogus")
    assert isinstance(Verdict("unknown").as_meta(), dict)
