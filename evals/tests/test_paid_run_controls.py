"""G2b/G3: engine LLM roles for the memspine arm, the price table and the dollar cap.

Every test runs on the stub LiteLLM transport: no request leaves the process.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.bedrock import (
    ENGINE_LLM_ROLES,
    QWEN3_32B,
    BudgetExceeded,
    CallBudget,
    merge_engine_llm_roles,
    parse_price,
)
from memspine_evals.cli import build_parser, parse_prices
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.experiments import (
    C01Config,
    build_systems,
    check_dollar_cap,
    price_table,
    run_c0_1,
)
from memspine_evals.results import read_run
from memspine_evals.runner import ModelCallBudgetExceeded
from memspine_evals.stub_llm import StubLiteLLM, install_stub_litellm

#: Engine overrides for fast offline engines: hash embeddings, mining on.
MINING = {
    "embedding": {"provider": "hash"},
    "memories": {
        "episodic": {
            "enabled": True,
            # the synthetic turns are a day apart: one session needs a wide gap
            "policies": {
                "consolidation": {
                    "mine_facts": True,
                    "min_session_records": 2,
                    "session_gap_minutes": 100_000,
                }
            },
        },
        "semantic": {"enabled": True},
    },
}


def _rows(out: Path, run_id: str) -> list[dict[str, Any]]:
    _, rows, _ = read_run(out / run_id / "results.jsonl")
    return rows


def _summary(out: Path, run_id: str) -> dict[str, Any]:
    return json.loads((out / run_id / "summary.json").read_text(encoding="utf-8"))


# -- G2b: engine roles --------------------------------------------------------


def test_merge_binds_every_engine_role_and_keeps_explicit_ones() -> None:
    explicit = {"llm": {"roles": {"extract": {"model": "ollama/llama3"}}}, "read": {"x": 1}}
    merged = merge_engine_llm_roles(explicit, QWEN3_32B, "eu-west-1")
    roles = merged["llm"]["roles"]
    assert set(roles) == set(ENGINE_LLM_ROLES)
    assert roles["extract"] == {"model": "ollama/llama3"}  # the caller's binding wins
    assert roles["reflect"] == {"model": QWEN3_32B, "aws_region": "eu-west-1"}
    assert merged["read"] == {"x": 1}
    assert explicit["llm"]["roles"] == {"extract": {"model": "ollama/llama3"}}  # not mutated


def test_engine_role_list_matches_the_engine() -> None:
    from memspine.prompts.roles import PROMPT_ROLES

    assert set(ENGINE_LLM_ROLES) <= set(PROMPT_ROLES)


def test_build_systems_binds_qwen3_roles_in_env_region(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_REGION_NAME", "us-west-2")
    config = C01Config(
        include_memspine=True, memspine_llm="bedrock-qwen3", memspine_config={"read": {}}
    )
    memspine = next(s for s in build_systems(config) if s.system_id == "memspine")
    roles = memspine.describe()["config"]["llm"]["roles"]
    assert {r: b["model"] for r, b in roles.items()} == dict.fromkeys(ENGINE_LLM_ROLES, QWEN3_32B)
    assert {b["aws_region"] for b in roles.values()} == {"us-west-2"}
    plain = next(
        s for s in build_systems(C01Config(include_memspine=True)) if s.system_id == "memspine"
    )
    assert "llm" not in plain.describe()["config"]


def test_cli_flags_parse() -> None:
    args = build_parser().parse_args(
        [
            "c0-1",
            "--dataset",
            "synthetic",
            "--memspine-llm",
            "bedrock-qwen3",
            "--price",
            f"{QWEN3_32B}=0.15,0.60",
            "--price",
            "other=1,2",
            "--max-usd",
            "12.5",
        ]
    )
    assert args.memspine_llm == "bedrock-qwen3" and args.max_usd == 12.5
    assert parse_prices(args.price) == ((QWEN3_32B, 0.15, 0.60), ("other", 1.0, 2.0))
    with pytest.raises(SystemExit):
        parse_prices(["no-equals-sign"])
    with pytest.raises(ValueError):
        parse_price("m=1")
    with pytest.raises(ValueError):
        parse_price("m=-1,2")


def test_price_table_overrides_defaults() -> None:
    table = price_table(C01Config(prices_per_mtok=((QWEN3_32B, 0.15, 0.60),)))
    assert table[QWEN3_32B] == (0.15, 0.60)


def test_dollar_cap_needs_prices_and_bedrock() -> None:
    with pytest.raises(ValueError, match="Bedrock"):
        check_dollar_cap(C01Config(mode="qa", max_usd=1.0))
    with pytest.raises(ValueError, match="no paid model"):
        check_dollar_cap(C01Config(mode="retrieval", max_usd=1.0))
    explicit = {"llm": {"roles": {"extract": {"model": "openai/unpriced"}}}}
    with pytest.raises(ValueError, match="openai/unpriced"):
        check_dollar_cap(
            C01Config(
                include_memspine=True,
                memspine_llm="bedrock-qwen3",
                memspine_config=explicit,
                max_usd=1.0,
            )
        )
    check_dollar_cap(C01Config(mode="qa", bedrock=True, max_usd=1.0, max_model_calls=10))


# -- G3: the meter ------------------------------------------------------------


def test_call_budget_refuses_the_call_that_would_cross_the_cap() -> None:
    budget = CallBudget(max_calls=100, prices_per_mtok={QWEN3_32B: (1.0, 1.0)}, max_usd=0.001)
    budget.reserve(QWEN3_32B, 400, 256)  # worst case $0.000656: allowed
    budget.record(QWEN3_32B, 400, 10)
    with pytest.raises(BudgetExceeded, match="dollar cap"):
        budget.reserve(QWEN3_32B, 400, 256)  # 0.00041 + 0.000656 > 0.001
    assert budget.calls == 1


def test_engine_charges_count_and_stop_the_next_call() -> None:
    budget = CallBudget(max_calls=5, prices_per_mtok={QWEN3_32B: (1.0, 1.0)}, max_usd=0.01)
    budget.charge(QWEN3_32B, 9_000, 2_000, calls=3)  # observed after the fact: never raises
    assert budget.calls == 3 and budget.engine_calls == 3
    assert budget.spent_usd() == pytest.approx(0.011)
    with pytest.raises(BudgetExceeded):
        budget.check_usd()
    assert issubclass(BudgetExceeded, ModelCallBudgetExceeded)


def test_tiny_max_usd_stops_early_with_unattempted_rows(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=2, turns_per_item=12, facts_per_item=3)
    config = C01Config(
        mode="qa",
        bedrock=True,
        max_model_calls=1000,
        judge_prompt="rubric",
        only_systems=("verbatim-bm25",),
        prices_per_mtok=((QWEN3_32B, 1000.0, 1000.0),),  # inflated so a few calls hit the cap
        max_usd=2.0,
    )
    with install_stub_litellm() as stub, pytest.raises(BudgetExceeded):
        asyncio.run(run_c0_1(dataset, config, tmp_path, run_id="cap"))
    rows = _rows(tmp_path, "cap--verbatim-bm25")
    statuses = [r["status"] for r in rows]
    assert "error" not in statuses
    assert statuses.count("completed") >= 1 and statuses.count("unattempted") >= 1
    assert len(rows) == sum(len(item.queries) for item in dataset.items())
    summary = _summary(tmp_path, "cap--verbatim-bm25")
    assert summary["spend"]["usd"] <= 2.0  # the pre-call worst case never let it cross
    assert summary["manifest"]["limits"]["max_usd"] == 2.0
    assert stub.calls["reader"] >= 1


def test_engine_calls_are_charged_and_capped(tmp_path: Path) -> None:
    """Engine-side LLM calls (mining at build) are charged to the arm's meter as
    observed; the cap then stops the next call, so later questions are UNATTEMPTED."""
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = C01Config(
        mode="retrieval",
        include_memspine=True,
        only_systems=("memspine",),
        memspine_llm="bedrock-qwen3",
        memspine_config=MINING,
        memspine_build_sleep=True,
        max_model_calls=1000,
        prices_per_mtok=((QWEN3_32B, 1e6, 1e6),),  # every engine token costs $1
        max_usd=1.0,
    )
    with install_stub_litellm() as stub, pytest.raises(BudgetExceeded):
        asyncio.run(run_c0_1(dataset, config, tmp_path, run_id="eng"))
    assert stub.calls["extract_session"] >= 1
    rows = _rows(tmp_path, "eng--memspine")
    assert rows and {r["status"] for r in rows} == {"unattempted"}
    summary = _summary(tmp_path, "eng--memspine")
    usage = summary["engine_llm_usage"]["K"]["extract"]
    assert usage["model"] == QWEN3_32B and usage["calls"] >= 1 and usage["prompt"] > 0
    # binding summarize also turns consolidation summaries into LLM calls
    assert summary["engine_llm_usage"]["K"]["summarize"]["calls"] >= 1
    k_calls = sum(u["calls"] for u in summary["engine_llm_usage"]["K"].values())
    assert summary["spend"]["engine_calls"] == k_calls
    assert summary["manifest"]["labels"]["memspine_llm"] == "bedrock-qwen3"
    assert summary["manifest"]["labels"]["engine_llm_models"] == [QWEN3_32B]


def test_engine_prompt_usage_is_attributed_per_stage(tmp_path: Path) -> None:
    """#33: the summary splits the engine's LLM use per loop stage AND per prompt
    version, read from ``Engine.usage()``; its calls match the per-role tally."""
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = C01Config(
        mode="retrieval",
        include_memspine=True,
        only_systems=("memspine",),
        memspine_llm="bedrock-qwen3",
        memspine_config=MINING,
        memspine_build_sleep=True,
        max_model_calls=1000,
    )
    with install_stub_litellm():
        asyncio.run(run_c0_1(dataset, config, tmp_path, run_id="cpc"))
    summary = _summary(tmp_path, "cpc--memspine")
    prompts = summary["engine_prompt_usage"]["K"]
    assert any(key.startswith("extract@") for key in prompts)
    assert all(entry["prompt_id"] for entry in prompts.values())
    by_prompt = sum(entry["calls"] for entry in prompts.values())
    by_role = sum(u["calls"] for u in summary["engine_llm_usage"]["K"].values())
    assert by_prompt == by_role


def test_engine_calls_count_against_the_call_cap(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=1, turns_per_item=8, facts_per_item=2)
    config = C01Config(
        mode="retrieval",
        include_memspine=True,
        only_systems=("memspine",),
        memspine_llm="bedrock-qwen3",
        memspine_config=MINING,
        memspine_build_sleep=True,
        max_model_calls=0,
    )
    with install_stub_litellm(), pytest.raises(ModelCallBudgetExceeded):
        asyncio.run(run_c0_1(dataset, config, tmp_path, run_id="calls"))
    assert {r["status"] for r in _rows(tmp_path, "calls--memspine")} == {"unattempted"}


def test_engine_llm_without_call_cap_is_refused(tmp_path: Path) -> None:
    config = C01Config(include_memspine=True, memspine_llm="bedrock-qwen3")
    with pytest.raises(ValueError, match="max_model_calls"):
        asyncio.run(run_c0_1(SyntheticDataset(n_items=1), config, tmp_path))


def test_stub_mimics_qwen3_thinking() -> None:
    stub = StubLiteLLM()

    async def go(content: str) -> str:
        reply = await stub.acompletion(messages=[{"role": "user", "content": content}])
        return str(reply.choices[0].message.content)

    assert asyncio.run(go("Question: x\nAnswer:")).startswith("<think>")
    assert not asyncio.run(go("Question: x\nAnswer: /no_think")).startswith("<think>")
