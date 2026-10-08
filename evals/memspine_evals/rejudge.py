"""N57: re-grade a finished QA run's saved answers with other judges (no new answers).

Vendor LoCoMo numbers come from vendor judges ("be generous", partial credit, 14-day date
tolerance). Re-grading memspine's own saved answers with each vendor's judge separates the
judge's leniency from the memory system: the answers stay fixed and only the grader changes.

    python -m memspine_evals.rejudge --run runs/<run-id>/results.jsonl \\
        --suites mem0-generous,mem0-unified,evermemos,omnimemeval --dry-run
    python -m memspine_evals.rejudge --run ... --suites ... --max-model-calls 7000 \\
        --price bedrock/converse/qwen.qwen3-32b-v1:0=0.15,0.60 --max-usd 3

``--dry-run`` makes no model call: it prints the call count and a token estimate
(characters / 4). A real run writes ``rejudge.jsonl`` (one row per question and suite;
re-running resumes, skipping rows already graded) and ``rejudge_summary.json`` beside the run.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from .bedrock import QWEN3_32B, CallBudget, litellm_chat, parse_price
from .judge_prompts import JUDGE_SUITES, RoutedLLMJudge

__all__ = ["load_answers", "main", "summarise"]

#: The vendor judges ask for a one-sentence reason before the label; 96 tokens (the
#: rubric judge's cap) can cut the label off.
MAX_TOKENS = 192


def load_answers(path: Path) -> list[dict[str, Any]]:
    """The completed, gold-bearing result rows of one run."""
    rows = []
    with path.open(encoding="utf8") as fh:
        for line in fh:
            row = json.loads(line)
            if row.get("kind") != "result" or row.get("status") != "completed":
                continue
            if row.get("gold") in (None, "") or row.get("answer") is None:
                continue
            rows.append(row)
    return rows


def _key(row: dict[str, Any], suite: str) -> str:
    return f"{row['run_id']}|{row['item_id']}|{row['query_id']}|{suite}"


def summarise(graded: list[dict[str, Any]]) -> dict[str, Any]:
    """Accuracy per suite and run, overall and per category, plus the original judge's."""
    acc: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    orig: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    flips: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # suite -> [up, down]
    for g in graded:
        if g.get("score") is None:
            continue
        for cat in ("all", g["type_label"]):
            acc[f"{g['suite']}|{g['run_id']}"][cat].append(g["score"])
            orig[g["run_id"]][cat].append(g["original_score"])
        if g["score"] > g["original_score"]:
            flips[g["suite"]][0] += 1
        elif g["score"] < g["original_score"]:
            flips[g["suite"]][1] += 1

    def pct(d: dict[str, list[float]]) -> dict[str, Any]:
        return {c: {"acc": round(100 * sum(v) / len(v), 2), "n": len(v)} for c, v in d.items()}

    return {
        "by_suite_run": {k: pct(v) for k, v in sorted(acc.items())},
        "original_by_run": {k: pct(v) for k, v in sorted(orig.items())},
        "flips_vs_original": {
            s: {"wrong_to_correct": u, "correct_to_wrong": d} for s, (u, d) in sorted(flips.items())
        },
        "errors": sum(1 for g in graded if g.get("score") is None),
    }


async def _grade_all(
    rows: list[dict[str, Any]],
    suites: list[str],
    chat: Any,
    model: str,
    out: Path,
    done: set[str],
    concurrency: int,
) -> None:
    judges = {s: RoutedLLMJudge(chat, model=model, suite=s) for s in suites}
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def one(row: dict[str, Any], suite: str) -> None:
        async with sem:
            rec = {
                "run_id": row["run_id"],
                "item_id": row["item_id"],
                "query_id": row["query_id"],
                "type_label": row.get("type_label") or "",
                "suite": suite,
                "original_score": row["score"],
                "original_prompt_id": (row.get("meta") or {}).get("judge_prompt_id"),
            }
            try:
                verdict = await judges[suite].score(row["question"], row["answer"], row["gold"])
                rec.update(
                    score=verdict.score, raw=verdict.raw, prompt_id=verdict.meta.get("prompt_id")
                )
            except Exception as exc:  # an ungradable reply is a missing measurement
                rec.update(score=None, error=f"{type(exc).__name__}: {exc}")
            async with lock, out.open("a", encoding="utf8") as fh:
                fh.write(json.dumps(rec) + "\n")

    tasks = [one(r, s) for r in rows for s in suites if _key(r, s) not in done]
    await asyncio.gather(*tasks)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memspine_evals.rejudge", description=__doc__)
    ap.add_argument(
        "--run",
        action="append",
        required=True,
        type=Path,
        help="a results.jsonl (repeat for several runs)",
    )
    ap.add_argument("--suites", required=True, help=f"comma list from {sorted(JUDGE_SUITES)}")
    ap.add_argument("--model", default=QWEN3_32B)
    ap.add_argument(
        "--out-dir", type=Path, default=None, help="default: runs/rejudge-<first run id>"
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max-model-calls", type=int, default=None)
    ap.add_argument("--max-usd", type=float, default=None)
    ap.add_argument("--price", action="append", default=[])
    ap.add_argument("--concurrency", type=int, default=8)
    args = ap.parse_args(argv)

    suites = [s.strip() for s in args.suites.split(",") if s.strip()]
    for s in suites:
        if s not in JUDGE_SUITES:
            ap.error(f"unknown suite {s!r}")
    rows = [r for p in args.run for r in load_answers(p)]
    out_dir = args.out_dir or args.run[0].parent.parent / f"rejudge-{rows[0]['run_id']}"
    out = out_dir / "rejudge.jsonl"

    done: set[str] = set()
    graded: list[dict[str, Any]] = []
    if out.exists():
        for line in out.read_text(encoding="utf8").splitlines():
            g = json.loads(line)
            if g.get("score") is not None:
                done.add(f"{g['run_id']}|{g['item_id']}|{g['query_id']}|{g['suite']}")

    pending = [(r, s) for r in rows for s in suites if _key(r, s) not in done]
    judges = {s: RoutedLLMJudge(lambda *a, **k: None, model=args.model, suite=s) for s in suites}
    chars = 0
    for r, s in pending:
        prompt = judges[s].prompts["default"]
        chars += len(prompt.render(r["question"], r["gold"], r["answer"])) + len(
            prompt.system or ""
        )
    est_in = chars // 4
    est_out = len(pending) * 60
    print(f"runs={len(args.run)} answers={len(rows)} suites={suites}")
    print(
        f"calls pending={len(pending)} (done {len(done)}); est. input tokens={est_in:,}, "
        f"output tokens~{est_out:,}"
    )
    if args.price:
        prices = {m: (i, o) for m, i, o in map(parse_price, args.price)}
        p_in, p_out = prices.get(args.model, (0.0, 0.0))
        print(f"est. cost ${est_in / 1e6 * p_in + est_out / 1e6 * p_out:.2f}")
    if args.dry_run:
        return 0
    if args.max_model_calls is None:
        ap.error("a real run needs --max-model-calls (no uncapped paid calls)")

    budget = CallBudget(
        max_calls=args.max_model_calls,
        prices_per_mtok={m: (i, o) for m, i, o in map(parse_price, args.price)} or None,
        max_usd=args.max_usd,
    )
    chat = litellm_chat(budget, model=args.model, max_tokens=MAX_TOKENS)
    out_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(_grade_all(rows, suites, chat, args.model, out, done, args.concurrency))

    graded = [json.loads(line) for line in out.read_text(encoding="utf8").splitlines()]
    summary = summarise(graded)
    summary["model"] = args.model
    summary["runs"] = [str(p) for p in args.run]
    summary["suites"] = {s: JUDGE_SUITES[s].notes for s in suites}
    summary["calls"] = budget.calls
    summary["spent_usd"] = round(budget.spent_usd(), 4)
    (out_dir / "rejudge_summary.json").write_text(json.dumps(summary, indent=2), encoding="utf8")
    print(json.dumps({k: v.get("all") for k, v in summary["by_suite_run"].items()}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
