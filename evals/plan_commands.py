"""Print the exact ``c0-1`` command for every arm of a run plan.

The plan (``plans/aamas_runs.json``) is the single source of truth for the paid
runs: ``rehearse.py`` checks it offline, and this script turns it into the
commands to run, so a handoff document can never drift from what was rehearsed.

    python plan_commands.py --path data/locomo10.json \
        --price bedrock/converse/qwen.qwen3-32b-v1:0=0.15,0.60 --max-usd 2 \
        [--arms H1,H5] [--repeat 3]

Prints one command per arm (and per repeat). Nothing is executed.
"""

from __future__ import annotations

import argparse
import json
import shlex
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

from memspine_evals.rehearsal import deep_merge

PLAN = Path(__file__).resolve().parent / "plans" / "aamas_runs.json"


def arm_command(
    plan: dict[str, Any],
    arm: dict[str, Any],
    *,
    path: str,
    prices: list[str],
    max_usd: float | None,
    max_calls: int,
    repeat: int | None,
) -> list[str]:
    proto = plan["protocol"]
    data = plan["dataset"]
    base = plan["memspine_base"]
    run_id = f"{plan['plan_id']}--{arm['id']}" + (f"--r{repeat}" if repeat else "")
    cmd = [
        "python", "-m", "memspine_evals", "c0-1",
        "--dataset", data["name"], "--path", path, "--revision", data["revision"],
        "--categories", ",".join(str(c) for c in data["categories"]),
        "--mode", proto["mode"], "--budget", str(proto["budget_tokens"]),
        "--top-k", str(proto["top_k"]),
        "--judge-prompt", proto["judge_prompt"],
        "--qa-prompt", arm.get("qa_prompt", proto["qa_prompt"]),
        "--max-model-calls", str(max_calls),
        "--run-id", run_id,
    ]  # fmt: skip
    if proto.get("bedrock"):
        cmd.append("--bedrock")
    if repeat:
        cmd += ["--seed", str(repeat)]
    systems = list(arm["systems"])
    if arm.get("dense"):
        cmd.append("--naive-dense-same-embedder")
    if arm.get("matched_budget_tokens"):
        cmd += ["--matched-budget-tokens", str(arm["matched_budget_tokens"])]
    if "memspine" in systems:
        cmd.append("--with-memspine")
        config = deep_merge(base.get("config", {}), arm.get("config_delta", {}))
        cmd += ["--memspine-config", json.dumps(config, separators=(",", ":"))]
        read_mode = arm.get("read_mode", base.get("read_mode"))
        if read_mode:
            cmd += ["--memspine-read-mode", read_mode]
        if arm.get("build_sleep", base.get("build_sleep")):
            cmd.append("--memspine-build-sleep")
        if proto.get("memspine_llm"):
            cmd += ["--memspine-llm", proto["memspine_llm"]]
    cmd += ["--only-systems", ",".join(systems)]
    for price in prices:
        cmd += ["--price", price]
    if max_usd is not None:
        cmd += ["--max-usd", str(max_usd)]
    return cmd


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--plan", default=str(PLAN))
    parser.add_argument("--path", default="data/locomo10.json")
    parser.add_argument("--arms", default=None, help="comma list of arm ids (default: all)")
    parser.add_argument("--price", action="append", default=[], metavar="MODEL=IN,OUT")
    parser.add_argument("--max-usd", type=float, default=None, help="cap per command")
    parser.add_argument("--max-model-calls", type=int, default=6000, help="cap per command")
    parser.add_argument("--repeat", type=int, default=0, help="emit N seeded repeats per arm")
    args = parser.parse_args(argv)

    plan = json.loads(Path(args.plan).read_text(encoding="utf-8"))
    wanted = set(args.arms.split(",")) if args.arms else None
    arms = [a for a in plan["arms"] if wanted is None or a["id"] in wanted]
    if wanted and len(arms) != len(wanted):
        missing = wanted - {a["id"] for a in arms}
        parser.error(f"unknown arm ids: {sorted(missing)}")
    for arm in arms:
        for rep in range(1, args.repeat + 1) if args.repeat else [None]:
            cmd = arm_command(
                plan,
                arm,
                path=args.path,
                prices=args.price,
                max_usd=args.max_usd,
                max_calls=args.max_model_calls,
                repeat=rep,
            )
            print(f"# {arm['id']}" + (f" (repeat {rep})" if rep else ""))
            print(shlex.join(cmd))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
