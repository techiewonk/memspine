"""Every command printed for the run plan parses with the real c0-1 CLI."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path

EVALS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS))

import plan_commands  # noqa: E402
from memspine_evals.cli import build_parser  # noqa: E402


def test_every_plan_arm_yields_a_parseable_command() -> None:
    plan = json.loads(plan_commands.PLAN.read_text(encoding="utf-8"))
    parser = build_parser()
    for arm in plan["arms"]:
        cmd = plan_commands.arm_command(
            plan, arm, path="data/locomo10.json", prices=["m=0.15,0.6"], max_usd=2.0,
            max_calls=6000, repeat=None,
        )  # fmt: skip
        args = parser.parse_args(cmd[3:])  # drop "python -m memspine_evals"
        assert args.categories == "1,2,3,4"
        assert args.max_usd == 2.0
        assert args.only_systems == ",".join(arm["systems"])
        if "memspine" in arm["systems"]:
            config = json.loads(args.memspine_config)
            assert config["read"]["record_access"] is False
            assert args.memspine_llm == "bedrock-qwen3"
        assert shlex.split(shlex.join(cmd)) == cmd


def test_repeats_get_distinct_run_ids_and_seeds() -> None:
    plan = json.loads(plan_commands.PLAN.read_text(encoding="utf-8"))
    arm = next(a for a in plan["arms"] if a["id"] == "memspine-base")
    one, two = (
        plan_commands.arm_command(
            plan, arm, path="p", prices=[], max_usd=None, max_calls=10, repeat=r
        )
        for r in (1, 2)
    )
    assert one[one.index("--run-id") + 1] != two[two.index("--run-id") + 1]
    assert one[one.index("--seed") + 1] == "1"


def test_every_arm_names_systems_the_harness_builds() -> None:
    """--only-systems must match real system ids, or the arm silently runs nothing."""
    from memspine_evals.experiments import C01Config, build_systems

    plan = json.loads(plan_commands.PLAN.read_text(encoding="utf-8"))
    parser = build_parser()
    for arm in plan["arms"]:
        cmd = plan_commands.arm_command(
            plan, arm, path="data/locomo10.json", prices=[], max_usd=None,
            max_calls=10, repeat=None,
        )  # fmt: skip
        args = parser.parse_args(cmd[3:])
        config = C01Config(
            include_memspine="memspine" in arm["systems"],
            naive_dense_same_embedder=bool(args.naive_dense_same_embedder),
            matched_budget_tokens=args.matched_budget_tokens,
        )
        built = {s.system_id for s in build_systems(config)}
        missing = set(arm["systems"]) - built
        assert not missing, f"{arm['id']}: {sorted(missing)} not in {sorted(built)}"


def test_rehearsal_builds_the_same_systems_as_the_command() -> None:
    """The rehearsal's C01Config must build every system the arm names (naive-dated)."""
    from memspine_evals.experiments import build_systems
    from memspine_evals.rehearsal import arm_config

    plan = json.loads(plan_commands.PLAN.read_text(encoding="utf-8"))
    for arm in plan["arms"]:
        config = arm_config(plan, arm, item_ids=None, max_queries=None, prices={})
        built = {s.system_id for s in build_systems(config)}
        missing = set(arm["systems"]) - built
        assert not missing, f"{arm['id']}: {sorted(missing)} not in {sorted(built)}"


def test_disabled_arms_run_only_when_named(capsys: object) -> None:
    import io
    from contextlib import redirect_stdout

    plan = json.loads(plan_commands.PLAN.read_text(encoding="utf-8"))
    disabled = [a["id"] for a in plan["arms"] if a.get("disabled")]
    assert "R-cohere" in disabled
    out = io.StringIO()
    with redirect_stdout(out):
        plan_commands.main([])
    assert "--run-id aamas27-locomo-qwen3--R-cohere " not in out.getvalue()
    out = io.StringIO()
    with redirect_stdout(out):
        plan_commands.main(["--arms", "R-cohere"])
    assert "--run-id aamas27-locomo-qwen3--R-cohere " in out.getvalue()
