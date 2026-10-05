"""plans/aamas_runs_extra.json: arm-level categories, parseable commands, valid configs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import plan_commands
import pytest
from memspine_evals.cli import build_parser
from memspine_evals.rehearsal import (
    arm_categories,
    arm_config,
    arm_engine_config,
    load_plan,
    validate_engine_config,
)

EXTRA = Path(plan_commands.__file__).resolve().parent / "plans" / "aamas_runs_extra.json"


def _plan() -> dict[str, Any]:
    return load_plan(EXTRA)


def _qa_prompt_choices(parser: argparse.ArgumentParser) -> set[str]:
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    c01 = sub.choices["c0-1"]
    action = next(a for a in c01._actions if "--qa-prompt" in a.option_strings)
    return set(action.choices or ())


def test_extra_plan_shares_the_main_plan_protocol() -> None:
    extra = _plan()
    main = json.loads(plan_commands.PLAN.read_text(encoding="utf-8"))
    assert extra["plan_id"] == main["plan_id"]
    for block in ("dataset", "protocol", "memspine_base"):
        assert extra[block] == main[block], block


def test_combo_a_config_is_copied_into_the_cat5_and_qwen3_arms() -> None:
    main = json.loads(plan_commands.PLAN.read_text(encoding="utf-8"))
    combo = next(a for a in main["arms"] if a["id"] == "combo-A")
    arms = {a["id"]: a for a in _plan()["arms"]}
    for arm_id in ("cat5-dated", "cat5-infer"):
        assert arms[arm_id]["config_delta"] == combo["config_delta"]
    assert arms["cat5-dated"]["qa_prompt"] == "dated"
    assert arms["cat5-infer"]["qa_prompt"] == "dated_infer"
    qwen = arms["R-qwen3"]["config_delta"]["read"]
    for key, value in combo["config_delta"]["read"].items():
        assert qwen[key] == value
    assert (qwen["rerank"], qwen["candidate_pool"]) == ("qwen3", 3)


def test_arm_categories_override_the_dataset_block() -> None:
    plan = _plan()
    arms = {a["id"]: a for a in plan["arms"]}
    assert arm_categories(plan, arms["cat5-dated"]) == (5,)
    assert arm_categories(plan, arms["H13"]) == (1, 2, 3, 4)
    config = arm_config(plan, arms["cat5-dated"], item_ids=None, max_queries=None, prices={})
    assert config.categories == (5,)


_ARM_IDS = [a["id"] for a in json.loads(EXTRA.read_text(encoding="utf-8"))["arms"]]


@pytest.mark.parametrize("arm_id", _ARM_IDS)
def test_every_extra_arm_yields_a_parseable_command(arm_id: str) -> None:
    plan = _plan()
    arm = next(a for a in plan["arms"] if a["id"] == arm_id)
    parser = build_parser()
    prompt = arm.get("qa_prompt", plan["protocol"]["qa_prompt"])
    if prompt not in _qa_prompt_choices(parser):
        pytest.skip(f"--qa-prompt {prompt} is not in this harness yet (added on another branch)")
    cmd = plan_commands.arm_command(
        plan, arm, path="data/locomo10.json", prices=[], max_usd=1.0, max_calls=10, repeat=None
    )
    args = parser.parse_args(cmd[3:])
    assert args.categories == ("5" if arm.get("categories") == [5] else "1,2,3,4")
    assert args.run_id == f"{plan['plan_id']}--{arm_id}"
    assert args.qa_prompt == prompt
    config = json.loads(args.memspine_config)
    assert config["read"]["record_access"] is False


def test_engine_configs_validate_and_rerank_mode_is_registered() -> None:
    from memspine.services.rerank.factory import rerank_modes

    plan = _plan()
    for arm in plan["arms"]:
        config = arm_engine_config(plan, arm)
        assert config is not None
        validate_engine_config(config)
        assert config.get("read", {}).get("rerank", "off") in rerank_modes()


def test_plan_commands_main_accepts_the_extra_plan(capsys: pytest.CaptureFixture[str]) -> None:
    assert plan_commands.main(["--plan", str(EXTRA), "--arms", "cat5-dated,H19"]) == 0
    out = capsys.readouterr().out
    assert "--categories 5 " in out and "--run-id aamas27-locomo-qwen3--H19" in out


def test_rehearse_loads_the_arm_categories_dataset(monkeypatch: pytest.MonkeyPatch) -> None:
    import asyncio

    from memspine_evals import rehearsal

    seen: dict[str, Any] = {}

    async def fake_arm(dataset: Any, plan: Any, arm: Any, out_dir: Any, **_: Any) -> Any:
        seen[arm["id"]] = dataset
        return rehearsal.ArmReport(arm_id=arm["id"], ok=True)

    monkeypatch.setattr(rehearsal, "rehearse_arm", fake_arm)
    monkeypatch.setattr(rehearsal, "dataset_shape", lambda *a, **k: {"questions": 1})
    plan = _plan()
    asked: list[tuple[int, ...] | None] = []

    def dataset_for(categories: tuple[int, ...] | None) -> str:
        asked.append(categories)
        return f"ds{categories}"

    asyncio.run(
        rehearsal.rehearse(
            plan, "ds-main", Path("."), item_ids=None, max_queries=None, prices={},
            arm_ids=("cat5-dated", "H13"), dataset_for=dataset_for,
        )
    )  # fmt: skip
    assert seen == {"cat5-dated": "ds(5,)", "H13": "ds-main"}
    assert asked == [(5,)]
