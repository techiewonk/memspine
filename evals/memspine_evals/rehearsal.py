"""Offline rehearsal of a paid run plan, and its cost projection (G5 + G6).

``python evals/rehearse.py --plan evals/plans/aamas_runs.json --path data/locomo10.json``

Each arm of the plan runs end to end on one LoCoMo conversation (or its first N
questions) exactly as the paid run would: the Bedrock reader and judge, the engine
with every LLM role bound to Qwen3, the call and dollar budgets. Only LiteLLM's
transport is replaced (:mod:`.stub_llm`), so nothing leaves the process.

Per arm it checks that

* the engine config parses (schema and every policy-option block);
* the run completes with no ERROR or UNATTEMPTED rows;
* every engine role the arm's feature needs was actually called
  (``Engine.model_usage`` per role, via ``summary.json``);
* no call reached the model without ``/no_think`` (the stub thinks otherwise).

It then projects the paid run (LoCoMo categories 1-4 by default): measured prompt
tokens per question (reader, judge, query-time engine roles) and per ingested turn
and session (write-time engine roles), with completion sizes from the plan's stated
assumptions, scaled to the full dataset and priced with ``--price``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .bedrock import QWEN3_32B, parse_price
from .experiments import C01Config, run_c0_1
from .stub_llm import StubLiteLLM, install_stub_litellm

__all__ = [
    "ArmReport",
    "arm_config",
    "deep_merge",
    "load_plan",
    "main",
    "policy_options",
    "project_arm",
    "rehearse",
    "validate_engine_config",
]

#: Placeholder prices (USD per 1M in / out) for the projection when none are given.
#: Verify on the AWS Bedrock pricing page for the region before any paid run.
PLACEHOLDER_PRICES: dict[str, tuple[float, float]] = {QWEN3_32B: (0.15, 0.60)}


def load_plan(path: str | Path) -> dict[str, Any]:
    plan = json.loads(Path(path).read_text(encoding="utf-8"))
    ids = [arm["id"] for arm in plan["arms"]]
    duplicates = sorted(i for i, n in Counter(ids).items() if n > 1)
    if duplicates:
        raise ValueError(f"duplicate arm ids in {path}: {duplicates}")
    return dict(plan)


def deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    """``delta`` over ``base``, recursively for nested mappings; neither is mutated."""
    out = dict(base)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def _has_memspine(arm: dict[str, Any]) -> bool:
    return "memspine" in arm.get("systems", ["memspine"])


def arm_categories(plan: dict[str, Any], arm: dict[str, Any]) -> tuple[int, ...] | None:
    """The arm's LoCoMo categories: an arm-level ``categories`` (a cat-5 safety arm)
    overrides the plan's dataset block."""
    return tuple(arm.get("categories") or plan["dataset"].get("categories") or ()) or None


def arm_engine_config(plan: dict[str, Any], arm: dict[str, Any]) -> dict[str, Any] | None:
    if not _has_memspine(arm):
        return None
    base = dict(plan.get("memspine_base", {}).get("config") or {})
    return deep_merge(base, arm.get("config_delta") or {})


def validate_engine_config(config: dict[str, Any]) -> None:
    """Parse ``config`` the way the engine will: the schema, then each policy block.

    Policy options (``read.scoring``, ``read.assembly``, ``memories.*.policies.*``)
    are open dicts in the schema and are only bound when a pipeline first runs, so a
    misspelt key there would otherwise pass start-up silently.
    """
    from memspine.config.schema import MemspineConfig
    from memspine.core.policies.assembly import AssemblyOptions
    from memspine.core.policies.scoring import ScoringOptions

    parsed = MemspineConfig.model_validate(config)
    if parsed.read.scoring:
        ScoringOptions.model_validate(parsed.read.scoring)
    if parsed.read.assembly:
        AssemblyOptions.model_validate(parsed.read.assembly)
    # C-8: every policy block, not only consolidation (conflict, dedup, decay, trust, ...)
    options = policy_options()
    blocks = [(f"memories.{n}", m.policies) for n, m in parsed.memories.items()]
    blocks += [(f"namespaces.{n}", ns.policies) for n, ns in _namespaces(parsed).items()]
    for where, policies in blocks:
        for name, raw in policies.items():
            model = options.get(name)
            if model is None:
                raise ValueError(
                    f"{where}.policies.{name}: unknown policy; known: {sorted(options)}"
                )
            model.model_validate(raw or {})


def _namespaces(parsed: Any) -> dict[str, Any]:
    namespaces = getattr(parsed, "namespaces", None)
    return dict(namespaces) if isinstance(namespaces, dict) else {}


def policy_options() -> dict[str, Any]:
    """Every shipped policy's name -> its ``Options`` model (``BindablePolicy`` subclasses
    in ``memspine.core.policies``), so a new policy is validated without listing it here."""
    import importlib
    import pkgutil

    import memspine.core.policies as package
    from memspine.core.policies.base import BindablePolicy

    for module in pkgutil.iter_modules(package.__path__):
        importlib.import_module(f"{package.__name__}.{module.name}")
    out: dict[str, Any] = {}
    stack = list(BindablePolicy.__subclasses__())
    while stack:
        cls = stack.pop()
        stack.extend(cls.__subclasses__())
        if getattr(cls, "name", ""):
            out[cls.name] = cls.Options
    return out


def arm_config(
    plan: dict[str, Any],
    arm: dict[str, Any],
    *,
    item_ids: tuple[str, ...] | None,
    max_queries: int | None,
    prices: dict[str, tuple[float, float]],
    max_model_calls: int = 1_000_000,
) -> C01Config:
    protocol = plan.get("protocol", {})
    base = plan.get("memspine_base", {})
    memspine = _has_memspine(arm)
    read_mode = arm["read_mode"] if "read_mode" in arm else base.get("read_mode")
    return C01Config(
        mode=protocol.get("mode", "qa"),
        bedrock=bool(protocol.get("bedrock", True)),
        budget_tokens=int(protocol.get("budget_tokens", 4096)),
        top_k=int(protocol.get("top_k", 10)),
        judge_prompt=protocol.get("judge_prompt", "rubric"),
        qa_prompt=arm.get("qa_prompt", protocol.get("qa_prompt", "default")),
        categories=arm_categories(plan, arm),
        include_memspine=memspine,
        memspine_config=arm_engine_config(plan, arm),
        memspine_read_mode=read_mode if memspine else None,
        memspine_build_sleep=bool(arm.get("build_sleep", base.get("build_sleep", False))),
        memspine_batch_turns=int(arm.get("batch_turns", base.get("batch_turns", 1))),
        memspine_llm=protocol.get("memspine_llm", "none") if memspine else "none",
        # The plan's ``dense`` flag is plan_commands' --naive-dense-same-embedder (the
        # naive-rag-dense-memspine-embedder system), not the c0-1 --dense retriever switch.
        naive_dense_same_embedder=bool(arm.get("dense", False)),
        matched_budget_tokens=arm.get("matched_budget_tokens"),
        only_systems=tuple(arm.get("systems", ["memspine"])),
        item_ids=item_ids,
        max_queries_per_item=max_queries,
        max_model_calls=max_model_calls,
        prices_per_mtok=tuple((m, p[0], p[1]) for m, p in prices.items()),
    )


# -- one arm -------------------------------------------------------------------


@dataclass
class SystemMeasure:
    """What one system of an arm spent on the rehearsal slice."""

    system_id: str
    n_queries: int
    statuses: dict[str, int]
    reader_calls: int
    reader_prompt: int
    judge_calls: int
    judge_prompt: int
    #: stage ("D" / "K" / "R") -> role -> {"model", "calls", "prompt", "completion"}
    engine: dict[str, dict[str, dict[str, Any]]]
    context_tokens_mean: float
    #: C-5: the run's rerank audit (``summary.json["rerank"]``)
    rerank: dict[str, Any] = field(default_factory=dict)


@dataclass
class ArmReport:
    arm_id: str
    ok: bool
    problems: list[str] = field(default_factory=list)
    systems: list[SystemMeasure] = field(default_factory=list)
    stub_calls: dict[str, int] = field(default_factory=dict)
    projection: dict[str, Any] = field(default_factory=dict)


def _measure(out_dir: Path, run_id: str, system_id: str) -> SystemMeasure:
    from .results import read_run

    folder = out_dir / f"{run_id}--{system_id}"
    payload = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
    _, rows, _ = read_run(folder / "results.jsonl")
    summary = payload["summary"]
    stage_g = summary["stages"]["G"]
    engine = payload.get("engine_llm_usage") or {}
    engine_prompt = sum(u["prompt"] for roles in engine.values() for u in roles.values())
    spend = payload.get("spend") or {}
    total_prompt = sum(t[0] for t in (spend.get("tokens") or {}).values())
    reader_prompt = int(stage_g["tokens_prompt"])
    return SystemMeasure(
        system_id=system_id,
        n_queries=len(rows),
        statuses=dict(Counter(str(r["status"]) for r in rows)),
        reader_calls=int(stage_g["calls"]),
        reader_prompt=reader_prompt,
        judge_calls=int(payload.get("judge_model_calls", 0)),
        judge_prompt=max(total_prompt - reader_prompt - engine_prompt, 0),
        engine=engine,
        context_tokens_mean=float(summary["context_tokens"].get("mean", 0.0) or 0.0),
        rerank=dict(payload.get("rerank") or {}),
    )


def _engine_role_calls(measure: SystemMeasure) -> Counter[str]:
    calls: Counter[str] = Counter()
    for roles in measure.engine.values():
        for role, used in roles.items():
            calls[role] += int(used.get("calls", 0))
    return calls


async def rehearse_arm(
    dataset: Any,
    plan: dict[str, Any],
    arm: dict[str, Any],
    out_dir: Path,
    *,
    item_ids: tuple[str, ...] | None,
    max_queries: int | None,
    prices: dict[str, tuple[float, float]],
) -> ArmReport:
    report = ArmReport(arm_id=arm["id"], ok=True)
    engine_config = arm_engine_config(plan, arm)
    try:
        if engine_config is not None:
            validate_engine_config(engine_config)
        config = arm_config(plan, arm, item_ids=item_ids, max_queries=max_queries, prices=prices)
    except Exception as exc:
        report.ok = False
        report.problems.append(f"config does not parse: {type(exc).__name__}: {exc}")
        return report
    run_id = f"rehearsal-{arm['id']}".replace("+", "_")
    stub = StubLiteLLM()
    try:
        with install_stub_litellm(stub):
            await run_c0_1(dataset, config, out_dir, run_id=run_id)
    except Exception as exc:
        report.ok = False
        report.problems.append(f"run failed: {type(exc).__name__}: {exc}")
        return report
    report.stub_calls = dict(stub.calls)
    if stub.reranks:
        report.stub_calls["rerank"] = stub.reranks
    if stub.thinking_calls:
        report.problems.append(f"{stub.thinking_calls} call(s) reached the model without /no_think")
    for system_id in config.only_systems or ():
        try:
            measure = _measure(out_dir, run_id, system_id)
        except FileNotFoundError:
            report.problems.append(f"system {system_id!r} produced no results (wrong id?)")
            continue
        report.systems.append(measure)
        bad = {s: n for s, n in measure.statuses.items() if s not in ("completed", "truncated")}
        if bad:
            report.problems.append(f"{system_id}: rows not completed: {bad}")
        if measure.n_queries == 0:
            report.problems.append(f"{system_id}: no questions ran")
        if measure.rerank.get("rerank_unavailable"):
            report.problems.append(
                f"{system_id}: a reranker is configured ({measure.rerank.get('mode')}) but "
                f"never returned scores ({measure.rerank.get('calls', 0)} call(s), "
                f"{measure.rerank.get('failures', 0)} failure(s))"
            )
        called = _engine_role_calls(measure)
        for role in arm.get("expect_roles", []):
            if not called.get(role):
                report.problems.append(f"{system_id}: feature role {role!r} was never called")
    report.ok = not report.problems
    return report


# -- projection ------------------------------------------------------------------


def _assumed(plan: dict[str, Any], key: str, caller: str, engine: bool = False) -> int:
    """Assumed completion tokens per call; the engine's judge role is judge_engine."""
    table = plan.get("projection_assumptions", {}).get(key, {})
    name = "judge_engine" if engine and caller == "judge" else caller
    return int(table.get(name, table.get("chat", 100)))


def project_arm(
    report: ArmReport,
    plan: dict[str, Any],
    prices: dict[str, tuple[float, float]],
    slice_shape: dict[str, int],
    full_shape: dict[str, int],
) -> dict[str, Any]:
    """Scale the rehearsal slice to the full run and price it.

    Query-time spend (reader, judge, R-stage engine roles) scales by questions;
    deposit-time engine spend (D) by ingested turns; synthesis (K, one call per
    session per stage) by sessions.
    """
    scale = {
        "q": full_shape["questions"] / max(slice_shape["questions"], 1),
        "D": full_shape["turns"] / max(slice_shape["turns"], 1),
        "K": full_shape["sessions"] / max(slice_shape["sessions"], 1),
        "R": full_shape["questions"] / max(slice_shape["questions"], 1),
    }
    lines: list[dict[str, Any]] = []
    for measure in report.systems:
        parts: list[tuple[str, str, float, float, float, float]] = []
        # (what, model, calls, prompt_tokens, expected_completion, ceiling_completion)
        for caller, n_calls, prompt in (
            ("reader", measure.reader_calls, measure.reader_prompt),
            ("judge", measure.judge_calls, measure.judge_prompt),
        ):
            n = n_calls * scale["q"]
            parts.append(
                (
                    caller,
                    QWEN3_32B,
                    n,
                    prompt * scale["q"],
                    n * _assumed(plan, "completion_tokens", caller),
                    n * _assumed(plan, "ceiling_completion_tokens", caller),
                )
            )
        for stage, roles in measure.engine.items():
            factor = scale.get(stage, scale["q"])
            for role, used in roles.items():
                n = int(used.get("calls", 0)) * factor
                parts.append(
                    (
                        f"engine:{role}@{stage}",
                        str(used.get("model") or QWEN3_32B),
                        n,
                        int(used.get("prompt", 0)) * factor,
                        n * _assumed(plan, "completion_tokens", role, engine=True),
                        n * _assumed(plan, "ceiling_completion_tokens", role, engine=True),
                    )
                )
        usd = usd_ceiling = 0.0
        calls = prompt_tokens = completion_tokens = 0.0
        breakdown: dict[str, float] = {}
        for what, model, n, p_tok, c_exp, c_max in parts:
            p_in, p_out = prices.get(model, (0.0, 0.0))
            cost = p_tok / 1e6 * p_in + c_exp / 1e6 * p_out
            usd += cost
            usd_ceiling += p_tok / 1e6 * p_in + c_max / 1e6 * p_out
            calls += n
            prompt_tokens += p_tok
            completion_tokens += c_exp
            breakdown[what] = round(cost, 4)
        lines.append(
            {
                "system_id": measure.system_id,
                "calls": round(calls),
                "prompt_tokens": round(prompt_tokens),
                "completion_tokens": round(completion_tokens),
                "usd": round(usd, 4),
                "usd_ceiling": round(usd_ceiling, 4),
                "breakdown_usd": breakdown,
            }
        )
    return {"scale": scale, "systems": lines}


def dataset_shape(
    dataset: Any, item_ids: tuple[str, ...] | None, max_queries: int | None
) -> dict[str, int]:
    """Questions, turns and sessions in the dataset (or the rehearsal slice of it)."""
    questions = turns = sessions = 0
    for item in dataset.items():
        if item_ids and item.item_id not in item_ids:
            continue
        n_q = len(item.queries)
        questions += min(n_q, max_queries) if max_queries is not None else n_q
        turns += len(item.history)
        sessions += len({t.session_id for t in item.history})
    return {"questions": questions, "turns": turns, "sessions": sessions}


# -- driver ----------------------------------------------------------------------


async def rehearse(
    plan: dict[str, Any],
    dataset: Any,
    out_dir: Path,
    *,
    item_ids: tuple[str, ...] | None,
    max_queries: int | None,
    prices: dict[str, tuple[float, float]],
    arm_ids: tuple[str, ...] | None = None,
    dataset_for: Callable[[tuple[int, ...] | None], Any] | None = None,
) -> list[ArmReport]:
    """Rehearse every (selected) arm. ``dataset_for(categories)`` loads the dataset for
    an arm whose ``categories`` differ from the plan's (e.g. a cat-5 safety arm); without
    it every arm runs on ``dataset``."""
    plan_categories = tuple(plan["dataset"].get("categories") or ()) or None
    reports: list[ArmReport] = []
    for arm in plan["arms"]:
        if arm_ids and arm["id"] not in arm_ids:
            continue
        categories = arm_categories(plan, arm)
        arm_dataset = (
            dataset_for(categories)
            if dataset_for is not None and categories != plan_categories
            else dataset
        )
        slice_shape = dataset_shape(arm_dataset, item_ids, max_queries)
        full_shape = dataset_shape(arm_dataset, None, None)
        report = await rehearse_arm(
            arm_dataset,
            plan,
            arm,
            out_dir,
            item_ids=item_ids,
            max_queries=max_queries,
            prices=prices,
        )
        if report.systems:
            report.projection = project_arm(report, plan, prices, slice_shape, full_shape)
        reports.append(report)
        status = "PASS" if report.ok else "FAIL"
        print(f"[{status}] {report.arm_id}" + (f": {report.problems}" if report.problems else ""))
    return reports


def render_table(reports: list[ArmReport], prices: dict[str, tuple[float, float]]) -> str:
    price_note = ", ".join(f"`{m}` in ${p[0]}/M out ${p[1]}/M" for m, p in prices.items())
    lines = [
        f"Prices: {price_note}",
        "",
        "| arm | system | rehearsal | engine roles called | projected calls "
        "| prompt Mtok | completion Mtok | projected $ | ceiling $ |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    total = total_ceiling = 0.0
    for report in reports:
        problems = "; ".join(" ".join(p.split()) for p in report.problems)
        status = "PASS" if report.ok else f"FAIL: {problems[:300]}"
        rows = (report.projection or {}).get("systems") or [{}]
        for row in rows:
            measure = next((m for m in report.systems if m.system_id == row.get("system_id")), None)
            roles = (
                ", ".join(f"{r}:{n}" for r, n in sorted(_engine_role_calls(measure).items()))
                if measure
                else ""
            )
            total += row.get("usd", 0.0)
            total_ceiling += row.get("usd_ceiling", 0.0)
            lines.append(
                f"| {report.arm_id} | {row.get('system_id', '-')} | {status} | {roles or '-'} "
                f"| {row.get('calls', 0):,} | {row.get('prompt_tokens', 0) / 1e6:.2f} "
                f"| {row.get('completion_tokens', 0) / 1e6:.3f} | {row.get('usd', 0.0):.2f} "
                f"| {row.get('usd_ceiling', 0.0):.2f} |"
            )
    lines.append(f"| **total** | | | | | | | **{total:.2f}** | **{total_ceiling:.2f}** |")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rehearse", description=__doc__)
    parser.add_argument("--plan", required=True, help="run plan JSON (evals/plans/...)")
    parser.add_argument("--path", required=True, help="locomo10.json")
    parser.add_argument("--revision", default=None, help="dataset revision id (default: plan)")
    parser.add_argument("--items", type=int, default=1, help="conversations to rehearse on")
    parser.add_argument("--first", type=int, default=None, help="first N questions per item")
    parser.add_argument("--arms", default=None, help="comma list of arm ids (default: all)")
    parser.add_argument(
        "--price",
        action="append",
        default=None,
        metavar="MODEL=IN,OUT",
        help="USD per 1M tokens (repeatable). Default: PLACEHOLDER Qwen3-32B 0.15/0.60",
    )
    parser.add_argument("--out", default=None, help="output folder (default: a temp dir)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Belt and braces: a model download or hub lookup must fail, not reach the network.
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("AWS_REGION_NAME", "us-east-1")
    plan = load_plan(args.plan)
    prices = dict(PLACEHOLDER_PRICES)
    for spec in args.price or ():
        model, p_in, p_out = parse_price(spec)
        prices[model] = (p_in, p_out)

    from .datasets import LoCoMoDataset

    revision = args.revision or plan["dataset"].get("revision", "auto")
    loaded: dict[tuple[int, ...] | None, Any] = {}

    def dataset_for(categories: tuple[int, ...] | None) -> Any:
        if categories not in loaded:
            loaded[categories] = LoCoMoDataset(
                args.path, revision_id=revision, categories=categories
            )
        return loaded[categories]

    dataset = dataset_for(tuple(plan["dataset"].get("categories") or ()) or None)
    item_ids = tuple(item.item_id for item in dataset.items())[: args.items]
    if args.out:
        out_dir = Path(args.out)
    else:
        import tempfile

        out_dir = Path(tempfile.mkdtemp(prefix="memspine-rehearsal-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    arm_ids = tuple(args.arms.split(",")) if args.arms else None
    reports = asyncio.run(
        rehearse(
            plan,
            dataset,
            out_dir,
            item_ids=item_ids,
            max_queries=args.first,
            prices=prices,
            arm_ids=arm_ids,
            dataset_for=dataset_for,
        )
    )
    table = render_table(reports, prices)
    shape = {
        "slice": dataset_shape(dataset, item_ids, args.first),
        "full": dataset_shape(dataset, None, None),
        "item_ids": list(item_ids),
    }
    (out_dir / "rehearsal.json").write_text(
        json.dumps(
            {
                "plan": plan["plan_id"],
                "shape": shape,
                "prices": prices,
                "arms": [asdict(r) for r in reports],
            },
            indent=2,
            default=str,
        ),
        encoding="utf-8",
    )
    (out_dir / "PROJECTION.md").write_text(table + "\n", encoding="utf-8")
    print(f"\nslice: {shape['slice']}  full: {shape['full']}")
    print(table)
    print(f"\nwritten: {out_dir / 'rehearsal.json'}, {out_dir / 'PROJECTION.md'}")
    return 0 if all(r.ok for r in reports) else 1


if __name__ == "__main__":
    sys.exit(main())
