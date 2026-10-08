"""N51: on the measured read path, question order changes nothing.

A read under ``base`` must be side-effect free: no access boost, no cached answer, no
state a later question can see. ByteRover's and Dakera's leaderboard numbers depend on
question order (answer cache, recall-time boosts); memspine's must not.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
TURNS = [
    "Ana: I went camping by the lake last weekend",
    "Ben: I started a painting of a sunset",
    "Ana: we ran a charity race in May",
    "Ben: I joined a support group on Tuesday",
    "Ana: the kids loved the pottery class",
    "Ben: I am looking at adoption agencies",
    "Ana: my favourite book is The Hobbit",
    "Ben: we moved to a new flat in June",
]
QUESTIONS = [
    "What did Ana do in May 2023?",
    "When did Ben join the support group?",
    "What is Ana's favourite book?",
    "Where did Ana go camping?",
    "What is Ben painting?",
]


async def _answers(order: list[str], mode: str) -> dict[str, list[str]]:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    await eng.start()
    try:
        for i, text in enumerate(TURNS):
            await eng.write(
                text, namespace="u", memory_type="episodic", valid_from=T0 + timedelta(days=4 * i)
            )
        out: dict[str, list[str]] = {}
        for q in order:
            result = await eng.read(q, namespace="u", mode=mode, top_k=4, budget_tokens=400)
            out[q] = [r.content for r in result.context.records]
        return out
    finally:
        await eng.stop()


@pytest.mark.parametrize("mode", ["replay", "retrieve"])
async def test_reversed_question_order_gives_identical_contexts(mode: str) -> None:
    forward = await _answers(QUESTIONS, mode)
    backward = await _answers(list(reversed(QUESTIONS)), mode)
    assert forward == backward


async def test_asking_twice_gives_the_same_context() -> None:
    once = await _answers(QUESTIONS, "replay")
    twice = await _answers(QUESTIONS + QUESTIONS[:1], "replay")
    assert once[QUESTIONS[0]] == twice[QUESTIONS[0]]
