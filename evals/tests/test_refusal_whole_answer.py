"""I22: ``is_refusal`` matches only when the WHOLE answer is a refusal statement."""

from __future__ import annotations

import pytest
from memspine_evals.cli import build_parser
from memspine_evals.contracts import ReaderAnswer
from memspine_evals.refusal import (
    REFUSAL_MATCH_MODES,
    RefusalRetryReader,
    is_refusal,
)

REFUSALS = [
    "",
    "   ",
    "I do not know.",
    "I don't know",
    "Not mentioned in the conversation.",
    "Not mentioned",
    "Not specified.",
    "Not clear from the context.",
    "Unknown.",
    "There is no information about her age.",
    "There is no mention of a trip in the conversation.",
    "There is no record of that.",
    "The conversation does not mention it.",
    "The memories do not say.",
    "Melanie did not mention her age.",
    "I cannot determine this from the context.",
    "The answer cannot be determined from the memories.",
    "Sorry, I don't have that information.",
    "Based on the context, there is no record of that.",
    "Unfortunately, that is not mentioned in the conversation. There is no information on it.",
    "**I do not know.**",
]

# negative FACTS and answers that carry content: not refusals
ANSWERS = [
    "There is no school on Friday.",
    "There is no better place than Paris.",
    "There is no cure for the cold.",
    "I did not go to the party.",
    "I did not eat breakfast today.",
    "I do not like spicy food.",
    "I don't have a car.",
    "She has no siblings.",
    "No, she did not attend the wedding.",
    "Melanie did not go camping; she stayed home.",
    "Nothing was bought.",
    "She did not win.",
    "I do not know, but probably in May.",
    "Not mentioned directly, but likely 7 May 2023.",
    "Denver",
    "The Friday before 25 May 2023",
    "Likely yes, because she runs",
    "There is no traffic in the morning, so she walks to work at 8am.",
]


@pytest.mark.parametrize("text", REFUSALS)
def test_whole_answer_refusals(text: str) -> None:
    assert is_refusal(text), text


@pytest.mark.parametrize("text", ANSWERS)
def test_negative_facts_are_answers(text: str) -> None:
    assert not is_refusal(text), text


@pytest.mark.parametrize("text", ["There is no school on Friday.", "I did not go to the party."])
def test_legacy_mode_keeps_the_old_behaviour(text: str) -> None:
    assert is_refusal(text, "legacy")  # the defect: a negative fact counted as a refusal
    assert not is_refusal(text)


def test_legacy_matches_the_old_substring_rules() -> None:
    assert is_refusal("Melanie did not go.", "legacy")
    assert is_refusal("Well, there is no way to say.", "legacy")
    assert is_refusal("I do not know", "legacy") and is_refusal("", "legacy")
    assert not is_refusal("Denver", "legacy")


def test_unknown_mode_is_rejected() -> None:
    assert REFUSAL_MATCH_MODES == ("whole", "legacy")
    with pytest.raises(ValueError, match="mode"):
        is_refusal("x", "fuzzy")


class _Inner:
    reader_id = "inner"
    model = "m"

    def __init__(self, *texts: str) -> None:
        self.texts = list(texts)
        self.calls = 0

    def describe(self) -> dict[str, str]:
        return {"reader_id": self.reader_id}

    async def answer(
        self, question: str, context: str, question_date: str | None = None
    ) -> ReaderAnswer:
        self.calls += 1
        return ReaderAnswer(text=self.texts.pop(0), prompt_tokens=1, completion_tokens=1)


async def test_retry_reader_does_not_retry_a_negative_fact_by_default() -> None:
    inner = _Inner("There is no school on Friday.")
    out = await RefusalRetryReader(inner).answer("Is there school?", "memo")
    assert out.text == "There is no school on Friday." and inner.calls == 1


async def test_retry_reader_legacy_flag_retries_it() -> None:
    inner = _Inner("There is no school on Friday.", "Yes, on Monday.")
    reader = RefusalRetryReader(inner, refusal_match="legacy")
    out = await reader.answer("Is there school?", "memo")
    assert inner.calls == 2 and out.text == "Yes, on Monday."
    assert reader.describe()["refusal_match"] == "legacy"


def test_cli_flag_defaults_to_whole() -> None:
    assert build_parser().parse_args(["c0-1", "--dataset", "locomo"]).refusal_match == "whole"
    args = build_parser().parse_args(["c0-1", "--dataset", "locomo", "--refusal-match", "legacy"])
    assert args.refusal_match == "legacy"
