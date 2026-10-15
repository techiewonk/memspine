"""E06 slot verifier (``--verify-slots``): fake readers only, no model."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import Any

import pytest
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.experiments import C01Config, with_post_steps
from memspine_evals.slot_verify import SlotVerifyReader, repair_question

CTX = "[2023-05-01] Ana: I moved to Lisbon\n[2023-05-02] Ana: I adopted a dog on May 2"


class Fake:
    """A reader that answers from a function of the question text; counts calls."""

    reader_id = "fake"
    model = "fake-model"
    makes_model_calls = True

    def __init__(self, reply: Callable[[str], str], *, raw: str | None = None) -> None:
        self.reply = reply
        self.raw = raw
        self.questions: list[str] = []

    def describe(self) -> dict[str, Any]:
        return {"reader_id": self.reader_id}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        self.questions.append(question)
        return ReaderAnswer(
            text=self.reply(question),
            prompt_tokens=100,
            completion_tokens=10,
            latency_ms=5.0,
            model_calls=1,
            raw_text=self.raw,
        )


def first_then_fixed(first: str, fixed: str) -> Callable[[str], str]:
    return lambda q: fixed if "previous answer" in q else first


async def test_wrong_type_is_caught_and_repaired_with_cost_in_meta() -> None:
    inner = Fake(first_then_fixed("Ana adopted a dog.", "Ana adopted the dog on 2 May 2023."))
    reader = SlotVerifyReader(inner)
    out = await reader.answer("When did Ana adopt the dog?", CTX)
    assert out.text == "Ana adopted the dog on 2 May 2023."
    meta = out.extra_meta["slot_verify"]
    assert meta["outcome"] == "repaired"
    assert [d["kind"] for d in meta["defects"]] == ["missing_date"]
    assert meta["previous_answer"] == "Ana adopted a dog."
    assert meta["contract"]["answer_type"] == "date"
    # one bounded repair call, same memories, the defect named in the question text
    assert len(inner.questions) == 2
    assert "asks for a date" in inner.questions[1] and "Ana adopted a dog." in inner.questions[1]
    # cost accounting: both calls are in the answer and in the meta
    assert (out.model_calls, out.prompt_tokens, out.completion_tokens) == (2, 200, 20)
    assert meta["repair"]["model_calls"] == 1 and meta["repair"]["prompt_tokens"] == 100
    assert (reader.seen, reader.flagged, reader.repaired, reader.extra_calls) == (1, 1, 1, 1)


async def test_country_for_a_city_is_repaired() -> None:
    inner = Fake(first_then_fixed("Italy", "Rome"))
    out = await SlotVerifyReader(inner).answer("Which city did Ana move to?", CTX)
    assert out.text == "Rome"
    assert out.extra_meta["slot_verify"]["defects"][0]["kind"] == "wrong_granularity"


async def test_correct_answers_are_untouched_and_cost_nothing() -> None:
    inner = Fake(lambda q: "Ana adopted the dog on 2 May 2023.")
    reader = SlotVerifyReader(inner)
    out = await reader.answer("When did Ana adopt the dog?", CTX)
    assert out.text == "Ana adopted the dog on 2 May 2023."
    assert len(inner.questions) == 1
    assert (out.model_calls, out.prompt_tokens) == (1, 100)
    assert out.extra_meta["slot_verify"]["outcome"] == "clean"
    assert reader.extra_calls == 0


@pytest.mark.parametrize(
    "unknown",
    [
        "Not mentioned in the conversation.",
        "The memories do not state when Ana adopted the dog.",
        "It cannot be determined from the context.",
    ],
)
async def test_an_unknown_stays_unknown_and_is_never_re_asked(unknown: str) -> None:
    inner = Fake(lambda q: unknown)
    out = await SlotVerifyReader(inner).answer("When did Ana adopt the dog?", CTX)
    assert out.text == unknown
    assert len(inner.questions) == 1  # no repair call at all
    assert out.extra_meta["slot_verify"]["outcome"] == "skipped_abstention"


async def test_a_repair_that_turns_into_an_abstention_is_rejected() -> None:
    inner = Fake(first_then_fixed("A retriever.", "Not mentioned in the conversation."))
    out = await SlotVerifyReader(inner).answer("When did Ana adopt the dog?", CTX)
    assert out.text == "A retriever."  # the first answer stands
    meta = out.extra_meta["slot_verify"]
    assert meta["outcome"] == "rejected_abstention"
    assert out.model_calls == 2  # the spent call is still counted


async def test_a_repair_that_keeps_the_defect_is_rejected() -> None:
    inner = Fake(first_then_fixed("A retriever.", "A labrador."))
    reader = SlotVerifyReader(inner)
    out = await reader.answer("When did Ana adopt the dog?", CTX)
    assert out.text == "A retriever."
    assert out.extra_meta["slot_verify"]["outcome"] == "rejected_still_defective"
    assert reader.rejected == 1 and reader.repaired == 0


async def test_unknown_question_type_is_skipped() -> None:
    inner = Fake(lambda q: "Whatever.")
    out = await SlotVerifyReader(inner).answer("Tell me about Ana.", CTX)
    assert out.extra_meta["slot_verify"]["outcome"] == "skipped_unknown_type"
    assert len(inner.questions) == 1


async def test_list_completeness_from_the_readers_own_table() -> None:
    raw = (
        "Evidence:\n- painting at the studio\n- chess club\n- pottery class\n"
        "Answer: Painting and chess."
    )
    inner = Fake(first_then_fixed("Painting and chess.", "Painting, chess and pottery."), raw=raw)
    out = await SlotVerifyReader(inner).answer("What hobbies does Ana have?", CTX)
    assert out.text == "Painting, chess and pottery."
    meta = out.extra_meta["slot_verify"]
    assert (
        meta["defects"][0]["kind"] == "dropped_items" and "pottery" in meta["defects"][0]["detail"]
    )


async def test_count_that_disagrees_with_its_list() -> None:
    inner = Fake(
        first_then_fixed(
            "Ana has 3 pets: Rex, Tom, Bella, Max.", "Ana has 4 pets: Rex, Tom, Bella, Max."
        )
    )
    out = await SlotVerifyReader(inner).answer("How many pets does Ana have?", CTX)
    assert out.text.startswith("Ana has 4")
    assert out.extra_meta["slot_verify"]["defects"][0]["kind"] == "count_list_mismatch"


async def test_a_failing_repair_call_keeps_the_first_answer() -> None:
    def boom(q: str) -> str:
        if "previous answer" in q:
            raise RuntimeError("server down")
        return "A retriever."

    out = await SlotVerifyReader(Fake(boom)).answer("When did Ana adopt the dog?", CTX)
    assert out.text == "A retriever."
    assert out.extra_meta["slot_verify"]["outcome"] == "error"


async def test_inner_extra_meta_is_kept_and_soft_mode_acts_on_unnamed_answers() -> None:
    class Meta(Fake):
        async def answer(
            self, question: str, context: str, question_date: str | None = None
        ) -> ReaderAnswer:
            got = await super().answer(question, context, question_date)
            return replace(got, extra_meta={"other": 1})

    strict = await SlotVerifyReader(Meta(lambda q: "his mother")).answer(
        "Who gave Ana the dog?", CTX
    )
    assert (
        strict.extra_meta["other"] == 1 and strict.extra_meta["slot_verify"]["outcome"] == "clean"
    )
    inner = Meta(first_then_fixed("his mother", "Maria Lopez"))
    soft = await SlotVerifyReader(inner, "soft").answer("Who gave Ana the dog?", CTX)
    assert soft.text == "Maria Lopez"


def test_modes_and_describe() -> None:
    with pytest.raises(ValueError):
        SlotVerifyReader(Fake(lambda q: ""), "bogus")
    reader = SlotVerifyReader(Fake(lambda q: ""), "soft")
    assert reader.describe()["slot_verify"] == "soft"
    assert reader.reader_id == "fake+slots-soft"


def test_repair_question_names_the_defect_and_offers_the_unknown_path() -> None:
    q = repair_question(
        "When?", "A dog.", "the question asks for a date.", "The answer should be a date."
    )
    assert "A dog." in q and "asks for a date" in q and "Not mentioned in the conversation" in q


def test_off_by_default_and_wired_through_the_post_steps() -> None:
    base = Fake(lambda q: "x")
    assert with_post_steps(base, C01Config(), lambda p: base) is base
    wrapped = with_post_steps(base, C01Config(verify_slots="soft"), lambda p: base)
    assert isinstance(wrapped, SlotVerifyReader) and wrapped.mode == "soft"
    with pytest.raises(ValueError):
        with_post_steps(base, C01Config(verify_slots="nope"), lambda p: base)
