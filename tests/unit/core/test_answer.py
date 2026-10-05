"""#34: the final-answer extractor for reasoning chat prompts (``chat@dated3``)."""

from __future__ import annotations

import pytest

from memspine import Engine
from memspine.core.answer import final_answer


@pytest.mark.parametrize(
    ("reply", "answer"),
    [
        ("Line 3 names the second puppy.\nAnswer: Shadow", "Shadow"),
        ("Reasoning: Gina says dance is magical.\nAnswer: magical", "magical"),
        ("**Answer:** 7 May 2023", "7 May 2023"),
        ("The line from 2023-06-09 says last week.\n**Answer**: the week before 9 June 2023",
         "the week before 9 June 2023"),
        ("She said it twice. Final answer: 2", "2"),
        ("ANSWER\uff1a Paris", "Paris"),
        ("answer:pottery, camping", "pottery, camping"),
        # The LAST marker wins: reasoning may quote an earlier "answer:".
        ("I first thought answer: Coco, but line 5 is newer.\nAnswer: Shadow", "Shadow"),
        # Hidden reasoning is dropped, closed or not.
        ("<think>Answer: wrong</think>Line 2 says so.\nAnswer: right", "right"),
        ("thinking... Answer: wrong</think>\nAnswer: right", "right"),
        # A multi-line answer is kept whole.
        ("Two lines list them.\nAnswer:\n- pottery\n- camping", "- pottery\n- camping"),
    ],
)  # fmt: skip
def test_extracts_the_text_after_the_last_marker(reply: str, answer: str) -> None:
    assert final_answer(reply) == answer


@pytest.mark.parametrize(
    "reply",
    ["Shadow", "  Melanie went camping on 2023-07-14.  ", "The answer is 3", "Not mentioned"],
)
def test_a_reply_without_a_marker_comes_back_whole(reply: str) -> None:
    assert final_answer(reply) == reply.strip()


def test_marker_must_start_a_word() -> None:
    """``Reanswer:`` or ``my_answer:`` inside a word is not a marker."""
    assert final_answer("Reanswer: no") == "Reanswer: no"


def test_dangling_marker_falls_back_to_the_reasoning() -> None:
    assert final_answer("Melanie went camping.\nAnswer:") == "Melanie went camping."
    assert final_answer("Answer:") == "Answer:"


def test_engine_exposes_the_extractor() -> None:
    assert Engine.final_answer("Line 1.\nAnswer: Shadow") == "Shadow"
