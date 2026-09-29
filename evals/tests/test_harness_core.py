"""Token budgets, judges, metrics — the parts every run depends on."""

from __future__ import annotations

import asyncio

import pytest
from memspine_evals.judge import (
    ContainsJudge,
    ExactMatchJudge,
    JudgeScale,
    LLMJudge,
    recall_at_k,
    to_unit_interval,
)
from memspine_evals.metrics import CostModel, Ledger, Price, Stage, percentile
from memspine_evals.tokens import HeuristicTokenCounter, truncate_to_budget


def test_budget_truncation_keeps_the_head() -> None:
    counter = HeuristicTokenCounter()
    text = "alpha bravo charlie delta echo foxtrot golf hotel " * 20
    cut, tokens, truncated = truncate_to_budget(text, 10, counter)
    assert truncated is True
    assert tokens <= 10
    assert text.startswith(cut)


def test_text_inside_budget_is_untouched() -> None:
    counter = HeuristicTokenCounter()
    cut, tokens, truncated = truncate_to_budget("short text", 100, counter)
    assert (cut, truncated) == ("short text", False)
    assert tokens > 0


def test_scales_do_not_silently_convert() -> None:
    assert to_unit_interval(1.0, JudgeScale.BINARY) == 1.0
    assert to_unit_interval(3.0, JudgeScale.GRADED_15) == 0.5
    assert to_unit_interval(0.7, JudgeScale.GRADED_01) == 0.7


def test_deterministic_judges_make_no_model_calls() -> None:
    exact = ExactMatchJudge()
    contains = ContainsJudge()
    assert exact.spec.makes_model_calls is False
    v1 = asyncio.run(exact.score("q", "Lisbon", "lisbon"))
    v2 = asyncio.run(exact.score("q", "it is Lisbon", "Lisbon"))
    v3 = asyncio.run(contains.score("q", "it is Lisbon", "Lisbon"))
    assert (v1.score, v2.score, v3.score) == (1.0, 0.0, 1.0)
    assert v1.model_calls == 0


def test_missing_gold_is_skipped_not_scored_wrong() -> None:
    verdict = asyncio.run(ExactMatchJudge().score("q", "anything", None))
    assert verdict.meta["skipped"] == "no gold"


def test_llm_judge_rejects_an_ungradable_reply() -> None:
    async def chat(prompt: str) -> str:
        return "maybe?"

    judge = LLMJudge(chat, model="m", scale=JudgeScale.BINARY)
    with pytest.raises(ValueError, match="ungradable"):
        asyncio.run(judge.score("q", "a", "g"))


def test_llm_judge_enforces_its_declared_range() -> None:
    async def chat(prompt: str) -> str:
        return "4.5"

    judge = LLMJudge(chat, model="m", scale=JudgeScale.GRADED_01)
    with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
        asyncio.run(judge.score("q", "a", "g"))


def test_llm_judge_records_its_prompt_hash() -> None:
    async def chat(prompt: str) -> str:
        return "CORRECT"

    judge = LLMJudge(chat, model="gpt-4o", scale=JudgeScale.BINARY)
    assert judge.spec.prompt_hash and judge.spec.makes_model_calls
    verdict = asyncio.run(judge.score("q", "a", "g"))
    assert (verdict.score, verdict.model_calls) == (1.0, 1)


def test_retrieval_recall_is_never_faked() -> None:
    assert recall_at_k(("a", "b"), (), 5) is None  # no gold ids => unavailable
    assert recall_at_k(("a", "b"), ("b",), 5) == 1.0
    assert recall_at_k(("a", "b"), ("c",), 5) == 0.0
    assert recall_at_k(("a", "b", "c"), ("c",), 2) == 0.0


def test_cost_per_cycle_is_attributed_per_stage() -> None:
    ledger = Ledger()
    model = CostModel(model_prices={"gpt-4o": Price(prompt=2.5, completion=10.0)})
    ledger.add(Stage.DEPOSIT, calls=3, latency_ms=30)
    ledger.add(
        Stage.GENERATE,
        prompt_tokens=1000,
        completion_tokens=100,
        calls=1,
        cost_usd=model.cost("gpt-4o", 1000, 100),
    )
    assert ledger.stage(Stage.GENERATE).cost_usd == pytest.approx(3.5)
    assert ledger.total_model_calls == 4
    assert ledger.cost_per_cycle(10) == pytest.approx(0.35)
    assert ledger.to_dict()["D"]["calls"] == 3


def test_percentiles_on_small_samples() -> None:
    assert percentile([1.0], 0.95) == 1.0
    assert percentile([1.0, 2.0, 3.0], 0.5) == 2.0
