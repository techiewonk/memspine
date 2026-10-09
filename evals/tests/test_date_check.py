"""Gap A2: deterministic single-day date equivalence before the LLM judge."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from memspine_evals.date_check import date_equivalent
from memspine_evals.judge import GuardedJudge, JudgeScale


@pytest.mark.parametrize(
    ("gold", "answer"),
    [
        ("The Friday before 15 July 2023", "Last Friday, 2023-07-14."),
        ("The Tuesday before 20 July 2023", "She joined on Tuesday, 2023-07-18 (last Tuesday)."),
        ("The Friday before 22 October 2023", "on Friday, October 20, 2023."),
        ("7 May 2023", "Sunday, May 7, 2023"),
    ],
)
def test_same_single_day_is_equivalent(gold: str, answer: str) -> None:
    assert date_equivalent(gold, answer)


@pytest.mark.parametrize(
    ("gold", "answer"),
    [
        ("The sunday before 25 May 2023", "Saturday, 2023-05-20."),  # different day
        ("The weekend before 17 July 2023", "2023-07-15"),  # range gold: left to the judge
        ("2022", "in 2022"),  # year gold: left to the judge
        ("on 9 April, 2023", "Deborah never went; she went biking on 9 April, 2023."),  # denial
        ("7 May 2023", "I do not know."),
        ("Adoption agencies", "2023-05-07"),  # not a date gold
        ("7 May 2023", ""),
    ],
)
def test_not_equivalent(gold: str, answer: str) -> None:
    assert not date_equivalent(gold, answer)


class _Spec:
    judge_id, model, prompt_id, prompt_hash, makes_model_calls = "x", "m", "p", "h", True
    scale = JudgeScale.BINARY
    params: Any = ()


class _Inner:
    spec = _Spec()
    calls = 0

    async def score(self, question: str, answer: str, gold: str | None) -> Any:
        _Inner.calls += 1

        class V:
            score = 0.0

        return V()


def test_guarded_judge_credits_date_without_llm_call() -> None:
    judge = GuardedJudge(_Inner(), date_check=True)
    before = _Inner.calls
    verdict = asyncio.run(
        judge.score("When?", "Friday, 2023-07-14", "The Friday before 15 July 2023")
    )
    assert verdict.score == 1.0 and verdict.meta["guard"] == "date_equivalent"
    assert _Inner.calls == before
    asyncio.run(judge.score("When?", "Saturday, 2023-05-20", "The sunday before 25 May 2023"))
    assert _Inner.calls == before + 1  # no match: the LLM judge decides
    assert judge.spec.params["date_check"] == "date_check/v1"
