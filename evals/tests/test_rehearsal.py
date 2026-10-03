"""G5/G6: the offline rehearsal and its cost projection (stub transport, no network)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from memspine_evals.bedrock import QWEN3_32B
from memspine_evals.datasets.synthetic import SyntheticDataset
from memspine_evals.rehearsal import (
    ArmReport,
    SystemMeasure,
    arm_config,
    deep_merge,
    load_plan,
    project_arm,
    rehearse,
    validate_engine_config,
)

PLAN_PATH = Path(__file__).resolve().parents[1] / "plans" / "aamas_runs.json"
PRICES = {QWEN3_32B: (0.15, 0.60)}

#: A small plan on the synthetic dataset: one baseline, one engine feature arm,
#: and one arm whose expected role never runs (a rehearsal must catch that).
_EPISODIC = {
    "enabled": True,
    "policies": {
        "consolidation": {
            "mine_facts": True,
            "min_session_records": 2,
            "session_gap_minutes": 100_000,
        }
    },
}
SMALL_PLAN: dict[str, Any] = {
    "plan_id": "test",
    "dataset": {"categories": None},
    "protocol": {"mode": "qa", "bedrock": True, "memspine_llm": "bedrock-qwen3"},
    "memspine_base": {
        "read_mode": "replay",
        "config": {"embedding": {"provider": "hash"}, "read": {"record_access": False}},
    },
    "projection_assumptions": {"completion_tokens": {"reader": 20, "judge": 10, "extract": 500}},
    "arms": [
        {"id": "base", "systems": ["verbatim-bm25"]},
        {
            "id": "mining",
            "systems": ["memspine"],
            "build_sleep": True,
            "config_delta": {"memories": {"episodic": _EPISODIC}},
            "expect_roles": ["extract"],
        },
        {"id": "no-relevance", "systems": ["memspine"], "expect_roles": ["relevance"]},
    ],
}


def test_shipped_plan_parses_and_every_engine_config_validates() -> None:
    plan = load_plan(PLAN_PATH)
    ids = [arm["id"] for arm in plan["arms"]]
    for wanted in (
        "baselines",
        "memspine-base",
        "H1",
        "H3",
        "H11",
        "H4",
        "H5",
        "H16",
        "H12-dated",
        "H2",
        "H2+H6",
        "H8",
        "H14",
        "H17",
        "P4",
        "H15",
        "H21",
        "firewall-on",
        "dense-naive",
        "matched-budget",
    ):
        assert wanted in ids
    for arm in plan["arms"]:
        config = arm_config(plan, arm, item_ids=("conv-26",), max_queries=None, prices=PRICES)
        if config.include_memspine:
            assert config.memspine_config is not None
            validate_engine_config(config.memspine_config)
            assert config.memspine_llm == "bedrock-qwen3"


def test_validate_catches_misspelt_policy_keys() -> None:
    with pytest.raises(Exception, match="dedupe_jaccard"):
        validate_engine_config({"assembly": {"dedupe_jaccard": 0.8}})  # handoff's H23 slip
    with pytest.raises(Exception, match="mine_fact"):
        validate_engine_config(
            {"memories": {"episodic": {"policies": {"consolidation": {"mine_fact": True}}}}}
        )


def test_deep_merge_does_not_mutate() -> None:
    base = {"read": {"a": 1, "scoring": {"x": 0}}}
    merged = deep_merge(base, {"read": {"scoring": {"y": 1}}})
    assert merged == {"read": {"a": 1, "scoring": {"x": 0, "y": 1}}}
    assert base == {"read": {"a": 1, "scoring": {"x": 0}}}


def test_read_mode_null_overrides_the_base() -> None:
    plan = load_plan(PLAN_PATH)
    arms = {arm["id"]: arm for arm in plan["arms"]}
    kwargs: dict[str, Any] = {"item_ids": None, "max_queries": None, "prices": PRICES}
    assert arm_config(plan, arms["H2"], **kwargs).memspine_read_mode is None
    assert arm_config(plan, arms["H2+H6"], **kwargs).memspine_read_mode == "replay"
    assert arm_config(plan, arms["H3"], **kwargs).memspine_read_mode == "auto"
    assert arm_config(plan, arms["baselines"], **kwargs).memspine_llm == "none"


def test_projection_scales_by_questions_turns_and_sessions() -> None:
    measure = SystemMeasure(
        system_id="memspine",
        n_queries=10,
        statuses={"completed": 10},
        reader_calls=10,
        reader_prompt=10_000,
        judge_calls=10,
        judge_prompt=2_000,
        engine={"K": {"extract": {"model": QWEN3_32B, "calls": 2, "prompt": 4_000}}},
        context_tokens_mean=500.0,
    )
    report = ArmReport(arm_id="x", ok=True, systems=[measure])
    projection = project_arm(
        report,
        SMALL_PLAN,
        PRICES,
        slice_shape={"questions": 10, "turns": 100, "sessions": 2},
        full_shape={"questions": 1000, "turns": 1000, "sessions": 20},
    )
    row = projection["systems"][0]
    # reader 1000 calls: 1M prompt + 20k completion; judge 1000: 0.2M + 10k;
    # extract 20 calls (x10 sessions): 40k prompt + 10k completion
    assert row["calls"] == 2020
    assert row["prompt_tokens"] == 1_240_000
    assert row["completion_tokens"] == 40_000
    assert row["usd"] == pytest.approx(1.24 * 0.15 + 0.04 * 0.60, abs=0.01)


def test_rehearsal_runs_arms_offline_and_flags_missing_roles(tmp_path: Path) -> None:
    dataset = SyntheticDataset(n_items=2, turns_per_item=10, facts_per_item=2)
    first = next(iter(dataset.items())).item_id
    reports = asyncio.run(
        rehearse(SMALL_PLAN, dataset, tmp_path, item_ids=(first,), max_queries=2, prices=PRICES)
    )
    by_id = {r.arm_id: r for r in reports}
    assert by_id["base"].ok and by_id["mining"].ok, [r.problems for r in reports]
    assert not by_id["no-relevance"].ok
    assert "'relevance' was never called" in by_id["no-relevance"].problems[0]
    mining = by_id["mining"]
    assert mining.stub_calls.get("extract_session", 0) >= 1
    row = mining.projection["systems"][0]
    assert row["usd"] > 0 and "engine:extract@K" in row["breakdown_usd"]
    summary = json.loads(
        (tmp_path / "rehearsal-mining--memspine" / "summary.json").read_text(encoding="utf-8")
    )
    assert summary["spend"]["engine_calls"] >= 1
