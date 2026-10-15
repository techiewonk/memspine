"""I35: offline "second view" of a finished run under the Mem0-official LoCoMo judge.

Our own judge is not comparable to published LoCoMo numbers (judge choice alone moves a
system by up to 18 points). This tool turns a finished run's saved answers into the exact
prompts of the Mem0 J-score judge (``--judge-prompt mem0-official``) as a batch file. It makes
NO model call: a later GPU (or API) job answers the prompts, and ``collect`` scores the replies.

    # 1. prepare (CPU, free): one JSON line per gradable question
    python -m memspine_evals.second_view prepare --run evals/runs/<run>--memspine/results.jsonl \\
        --out evals/runs/<run>--memspine/second_view_mem0.batch.jsonl

    # 2. later, on the GPU box: answer each ``prompt`` (``system`` first) with the judge model,
    #    temperature 0, and write {"custom_id": ..., "reply": "<model text>"} per line.
    #    ``score`` below does it against any OpenAI-compatible endpoint (Ollama):
    python -m memspine_evals.second_view score --batch <batch.jsonl> --out <replies.jsonl> \\
        --base-url http://127.0.0.1:11434/v1 --model qwen3.5:9b

    # 3. collect (CPU, free): accuracy under the Mem0 judge, per category, beside the original
    python -m memspine_evals.second_view collect --batch <batch.jsonl> --replies <replies.jsonl> \\
        --out <summary.json>

``evals/run.sh``-compatible command (run from ``evals/``; ``PYTHON`` is the venv interpreter):

    "$PYTHON" -m memspine_evals.second_view prepare --run runs/<run-id>--memspine/results.jsonl \\
        --out runs/<run-id>--memspine/second_view_mem0.batch.jsonl

Scope. The Mem0 numbers are over LoCoMo categories 1-4 (the suite's ``handles_abstention`` is
false), so category 5 (and any row without gold or a completed answer) is left out. The prompt
text is the vendored Mem0 ``ACCURACY_PROMPT`` (see ``judge_prompts.JUDGE_SUITES['mem0-official']``
notes); the batch records its sha256 so the second view is tied to a prompt version.
Gold is read from the saved results, which is correct for a judge (not a run-time input).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from .judge_prompts import JUDGE_SUITES, RoutedLLMJudge

__all__ = ["collect", "main", "prepare"]

SUITE = "mem0-official"
EXCLUDED_LABELS = frozenset({"cat5", "adversarial"})


def _noop_chat(*_a: Any, **_k: Any) -> None:  # prompts are rendered, never sent
    return None


def _judge() -> RoutedLLMJudge:
    return RoutedLLMJudge(_noop_chat, model="second-view", suite=SUITE)


def custom_id(row: dict[str, Any]) -> str:
    return f"{row['run_id']}|{row['item_id']}|{row['query_id']}"


def prepare(results: Path, out: Path, *, include_cat5: bool = False) -> dict[str, Any]:
    """Write the batch file; return counts. Makes no model call."""
    judge = _judge()
    prompt = judge.prompts["default"]
    sha = judge.spec.prompt_hash
    kept = skipped = 0
    by_reason: dict[str, int] = defaultdict(int)
    out.parent.mkdir(parents=True, exist_ok=True)
    with results.open(encoding="utf-8") as src, out.open("w", encoding="utf-8") as dst:
        dst.write(json.dumps({
            "kind": "batch_manifest", "suite": SUITE, "prompt_hash": sha,
            "prompt_id": prompt.prompt_id, "source": str(results), "makes_model_calls": False,
            "suite_notes": JUDGE_SUITES[SUITE].notes,
            "reply_format": '{"custom_id": ..., "reply": "<judge text containing {\\"label\\": \\"CORRECT|WRONG\\"}>"}',
        }) + "\n")
        for line in src:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("kind") != "result":
                continue
            reason = None
            if row.get("status") != "completed":
                reason = "not_completed"
            elif row.get("gold") in (None, "") or row.get("answer") is None:
                reason = "no_gold_or_answer"
            elif not include_cat5 and row.get("type_label") in EXCLUDED_LABELS:
                reason = "cat5_abstention"
            if reason:
                skipped += 1
                by_reason[reason] += 1
                continue
            dst.write(json.dumps({
                "kind": "judge_request",
                "custom_id": custom_id(row),
                "run_id": row["run_id"], "item_id": row["item_id"], "query_id": row["query_id"],
                "type_label": row.get("type_label") or "",
                "original_score": row.get("score"),
                "system": prompt.system,
                "prompt": prompt.render(row["question"], str(row["gold"]), str(row["answer"])),
            }) + "\n")
            kept += 1
    return {"requests": kept, "skipped": skipped, "skipped_by_reason": dict(by_reason),
            "prompt_hash": sha, "out": str(out)}


def _read_batch(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest: dict[str, Any] = {}
    reqs: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if row.get("kind") == "batch_manifest":
            manifest = row
        elif row.get("kind") == "judge_request":
            reqs.append(row)
    return manifest, reqs


def collect(batch: Path, replies: Path, out: Path | None = None) -> dict[str, Any]:
    """Score the replies with the suite's own parser; accuracy overall and per category."""
    manifest, reqs = _read_batch(batch)
    prompt = _judge().prompts["default"]
    reply_by_id: dict[str, str] = {}
    for line in replies.read_text(encoding="utf-8").splitlines():
        if line.strip():
            r = json.loads(line)
            reply_by_id[r["custom_id"]] = r["reply"]
    acc: dict[str, list[float]] = defaultdict(list)
    orig: dict[str, list[float]] = defaultdict(list)
    unparsed = missing = 0
    for req in reqs:
        reply = reply_by_id.get(req["custom_id"])
        if reply is None:
            missing += 1
            continue
        try:
            score = float(prompt.parse_reply(reply))
        except Exception:
            unparsed += 1
            continue
        for key in ("ALL", req["type_label"]):
            acc[key].append(score)
            try:
                orig[key].append(float(req["original_score"]))
            except (TypeError, ValueError):
                pass
    summary = {
        "suite": manifest.get("suite", SUITE), "prompt_hash": manifest.get("prompt_hash"),
        "requests": len(reqs), "missing_replies": missing, "unparsed_replies": unparsed,
        "accuracy": {k: {"n": len(v), "mem0_official": sum(v) / len(v),
                         "original_judge": (sum(orig[k]) / len(orig[k])) if orig[k] else None}
                     for k, v in sorted(acc.items())},
    }
    if out:
        out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def _score(batch: Path, out: Path, base_url: str, model: str, max_tokens: int) -> int:
    """Answer the prompts with an OpenAI-compatible endpoint (GPU job). Resumes; temperature 0."""
    import urllib.request

    done = set()
    if out.exists():
        done = {json.loads(ln)["custom_id"] for ln in out.read_text(encoding="utf-8").splitlines() if ln.strip()}
    _, reqs = _read_batch(batch)
    with out.open("a", encoding="utf-8") as fh:
        for req in reqs:
            if req["custom_id"] in done:
                continue
            messages = ([{"role": "system", "content": req["system"]}] if req.get("system") else []) + [
                {"role": "user", "content": req["prompt"]}]
            body = json.dumps({"model": model, "messages": messages, "temperature": 0,
                               "max_tokens": max_tokens}).encode()
            http = urllib.request.Request(base_url.rstrip("/") + "/chat/completions", body,
                                          {"Content-Type": "application/json"})
            with urllib.request.urlopen(http, timeout=300) as resp:  # noqa: S310 - operator-supplied URL
                text = json.loads(resp.read())["choices"][0]["message"]["content"]
            fh.write(json.dumps({"custom_id": req["custom_id"], "reply": text}) + "\n")
            fh.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="memspine_evals.second_view", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("prepare", help="write the judge-prompt batch (no model call)")
    p.add_argument("--run", type=Path, required=True, help="a finished run's results.jsonl")
    p.add_argument("--out", type=Path, required=True)
    p.add_argument("--include-cat5", action="store_true")
    s = sub.add_parser("score", help="GPU job: answer the batch via an OpenAI-compatible endpoint")
    s.add_argument("--batch", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True)
    s.add_argument("--base-url", default="http://127.0.0.1:11434/v1")
    s.add_argument("--model", required=True)
    s.add_argument("--max-tokens", type=int, default=192)
    c = sub.add_parser("collect", help="score replies and summarise (no model call)")
    c.add_argument("--batch", type=Path, required=True)
    c.add_argument("--replies", type=Path, required=True)
    c.add_argument("--out", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "prepare":
        print(json.dumps(prepare(args.run, args.out, include_cat5=args.include_cat5), indent=1))
    elif args.cmd == "score":
        return _score(args.batch, args.out, args.base_url, args.model, args.max_tokens)
    else:
        print(json.dumps(collect(args.batch, args.replies, args.out), indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
