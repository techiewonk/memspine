"""G01: a retry only for a diagnosed defect (``--verify-slots diagnosed``). Fake readers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from memspine_evals.cli import build_parser
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.defect_diagnosis import (
    arithmetic_mismatch,
    diagnose,
    duration_mismatch,
    refusal_evidence,
    supported_by,
    unsupported_claims,
)
from memspine_evals.slot_verify import SLOT_VERIFY_MODES, SlotVerifyReader

CTX = (
    "[2023-05-01] Ana: I moved to Lisbon for work\n"
    "[2023-05-02] Ana: I adopted a dog named Max on May 2\n"
    "[2023-05-09] Bob: I started painting landscapes"
)


class Fake:
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


def first_then(first: str, fixed: str) -> Callable[[str], str]:
    return lambda q: fixed if "previous answer" in q else first


# -- the diagnoses (pure) -----------------------------------------------------------------------


def test_unsupported_claims_names_only_phrases_the_context_lacks() -> None:
    assert unsupported_claims("Ana moved to Lisbon.", CTX, "Where did Ana move?") == []
    assert unsupported_claims("She went to Berlin with Carla.", CTX, "Where?") == ["Berlin", "Carla"]
    assert unsupported_claims('She read "Moby Dick".', CTX, "What?") == ["Moby Dick"]
    # a longer form of a name the context holds is supported; sentence-initial words and months are free
    assert unsupported_claims("Ana, in May, chose Lisbon Portugal.", CTX, "Where?") == []


def test_arithmetic_and_duration_checks() -> None:
    assert arithmetic_mismatch("12 - 5 = 8") and "is 7" in arithmetic_mismatch("12 - 5 = 8")  # type: ignore[operator]
    assert arithmetic_mismatch("3 + 4 = 7 and 2 x 3 = 6") is None
    assert duration_mismatch("About 3 weeks.", "from 2023-05-01 to 2023-05-22") is None
    msg = duration_mismatch("5 months", "from 2023-05-01 to 2023-05-22")
    assert msg and "21 days" in msg
    assert duration_mismatch("5 months", "only 2023-05-01 is named") is None  # needs two dates


def test_refusal_needs_a_line_with_every_subject_and_the_relation() -> None:
    assert refusal_evidence(CTX, ["Ana"], "adopted dog") == [
        "[2023-05-02] Ana: I adopted a dog named Max on May 2"
    ]
    assert refusal_evidence(CTX, ["Bob"], "adopted dog") == []  # Bob has no such line
    assert refusal_evidence(CTX, ["Ana"], "") == []  # no relation: nothing to diagnose
    assert refusal_evidence(CTX, ["Ana"], "climbed mountains") == []


def test_supported_by_needs_a_new_content_word_from_the_evidence() -> None:
    lines = ["[2023-05-02] Ana: I adopted a dog named Max on May 2"]
    assert supported_by("Ana adopted a dog called Max.", lines, "What did Ana adopt?")
    assert not supported_by("Ana adopted something.", lines, "What did Ana adopt?")
    assert not supported_by("A cat named Tom.", lines, "What did Ana adopt?")


def test_diagnose_routes_abstention_and_answer() -> None:
    kw = {"subjects": ["Ana"], "relation": "adopted dog"}
    a = diagnose("What did Ana adopt?", CTX, "Not mentioned.", abstained=True, **kw)
    assert [d.kind for d in a.defects] == ["unjustified_refusal"] and a.evidence
    b = diagnose("What did Ana adopt?", CTX, "A parrot named Zed.", abstained=False, **kw)
    assert [d.kind for d in b.defects] == ["unsupported_claim"]
    c = diagnose("How many?", CTX, "5", abstained=False, explanation="12 - 5 = 8", **kw)
    assert [d.kind for d in c.defects] == ["operand_result_mismatch"]


# -- the reader wrapper -------------------------------------------------------------------------


def test_mode_and_cli_choice() -> None:
    assert "diagnosed" in SLOT_VERIFY_MODES
    on = build_parser().parse_args(["c0-1", "--dataset", "locomo", "--verify-slots", "diagnosed"])
    assert on.verify_slots == "diagnosed"


async def test_unjustified_refusal_is_retried_with_the_evidence_and_kept_when_supported() -> None:
    inner = Fake(first_then("Not mentioned in the conversation.", "Ana adopted a dog named Max."))
    reader = SlotVerifyReader(inner, "diagnosed")
    out = await reader.answer("What did Ana adopt?", CTX)
    meta = out.extra_meta["slot_verify"]
    assert out.text == "Ana adopted a dog named Max." and meta["outcome"] == "repaired"
    assert [d["kind"] for d in meta["defects"]] == ["unjustified_refusal"]
    assert "I adopted a dog named Max" in inner.questions[1]  # the cited line is in the retry
    assert meta["diagnosis"]["kinds"] == ["unjustified_refusal"]
    assert meta["retry_cost"] == {"model_calls": 1, "prompt_tokens": 100, "completion_tokens": 10}
    assert (out.model_calls, out.prompt_tokens) == (2, 200)  # cost is in the row
    assert reader.diagnosed == {"unjustified_refusal": 1}


async def test_a_retry_that_guesses_does_not_replace_the_refusal() -> None:
    inner = Fake(first_then("Not mentioned in the conversation.", "Probably a cat."))
    out = await SlotVerifyReader(inner, "diagnosed").answer("What did Ana adopt?", CTX)
    assert out.text == "Not mentioned in the conversation."
    assert out.extra_meta["slot_verify"]["outcome"] == "rejected_unsupported"


async def test_a_retry_that_refuses_again_keeps_the_first() -> None:
    inner = Fake(lambda q: "Not mentioned in the conversation.")
    out = await SlotVerifyReader(inner, "diagnosed").answer("What did Ana adopt?", CTX)
    assert out.extra_meta["slot_verify"]["outcome"] == "rejected_abstention"
    assert len(inner.questions) == 2  # one bounded retry


async def test_an_evidence_based_unknown_is_never_retried() -> None:
    inner = Fake(lambda q: "Not mentioned in the conversation.")
    reader = SlotVerifyReader(inner, "diagnosed")
    out = await reader.answer("What did Bob adopt?", CTX)  # no Bob line holds the relation
    meta = out.extra_meta["slot_verify"]
    assert meta["outcome"] == "skipped_abstention" and meta["refusal_diagnosis"] == "no_evidence"
    assert len(inner.questions) == 1 and (out.model_calls, reader.extra_calls) == (1, 0)


async def test_strict_mode_still_never_touches_a_refusal() -> None:
    inner = Fake(lambda q: "Not mentioned in the conversation.")
    out = await SlotVerifyReader(inner, "strict").answer("What did Ana adopt?", CTX)
    assert out.extra_meta["slot_verify"]["outcome"].startswith("skipped") and len(inner.questions) == 1


async def test_unsupported_claim_is_retried_and_kept_only_when_fixed() -> None:
    inner = Fake(first_then("Ana adopted a parrot named Zed.", "Ana adopted a dog named Max."))
    out = await SlotVerifyReader(inner, "diagnosed").answer("What did Ana adopt?", CTX)
    meta = out.extra_meta["slot_verify"]
    assert out.text == "Ana adopted a dog named Max." and meta["outcome"] == "repaired"
    assert meta["defects"][0]["kind"] == "unsupported_claim"
    still = Fake(first_then("Ana adopted a parrot named Zed.", "It was a parrot named Zed."))
    out2 = await SlotVerifyReader(still, "diagnosed").answer("What did Ana adopt?", CTX)
    assert out2.text == "Ana adopted a parrot named Zed."
    assert out2.extra_meta["slot_verify"]["outcome"] == "rejected_still_defective"


async def test_operand_result_inconsistency_is_retried() -> None:
    inner = Fake(
        first_then("5", "7"),
        raw="The first amount is 12 and the second 5, so 12 - 5 = 8. Final answer: 5",
    )
    out = await SlotVerifyReader(inner, "diagnosed").answer("How many items are left?", CTX)
    meta = out.extra_meta["slot_verify"]
    assert meta["defects"][0]["kind"] == "operand_result_mismatch" and "is 7" in meta["defects"][0]["detail"]


async def test_clean_answers_cost_nothing_in_diagnosed_mode() -> None:
    inner = Fake(lambda q: "Ana adopted a dog named Max on 2 May 2023.")
    reader = SlotVerifyReader(inner, "diagnosed")
    out = await reader.answer("When did Ana adopt the dog?", CTX)
    assert out.extra_meta["slot_verify"]["outcome"] == "clean" and len(inner.questions) == 1
    assert (out.model_calls, reader.extra_calls, reader.diagnosed) == (1, 0, {})


async def test_default_modes_do_not_run_the_new_diagnoses() -> None:
    inner = Fake(lambda q: "Ana adopted a parrot named Zed on 2 May 2023.")
    for mode in ("strict", "soft"):
        out = await SlotVerifyReader(inner, mode).answer("When did Ana adopt the dog?", CTX)
        assert out.extra_meta["slot_verify"]["outcome"] == "clean"
