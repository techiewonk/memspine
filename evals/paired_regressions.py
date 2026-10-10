"""Offline paired comparison of two finished QA runs (C9): which previously-correct answers
turned wrong, and what they have in common. No model call, no gold at run time (this is a
post-hoc analysis of ``per_question.jsonl`` files).

    python evals/paired_regressions.py BASE_RUN FIXED_RUN [--runs-dir evals/runs] [--items conv-26,conv-30,conv-41,conv-42] [--show 12]

Reports per category: n, gained, lost, net; for the lost rows the question shape (list / count /
temporal / inference / other), answer length ratio, context size ratio, whether the new answer is
a refusal, and whether the gold evidence turn is still in the context (distractor vs retrieval).
Only the listed items are read (default: the four development conversations).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any

DEV_ITEMS = ("conv-26", "conv-30", "conv-41", "conv-42")


def load(run_dir: Path, items: tuple[str, ...]) -> dict[tuple[str, str], dict[str, Any]]:
    path = run_dir / "report" / "per_question.jsonl"
    rows: dict[tuple[str, str], dict[str, Any]] = {}
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            row = json.loads(line)
            if row["item"] in items:
                rows[(row["item"], row["qid"])] = row
    return rows


def _words(text: str) -> int:
    return len(re.findall(r"\S+", text or ""))


def _ctx_lines(row: dict[str, Any]) -> int:
    ctx = row.get("context_text")
    if isinstance(ctx, list):
        return len(ctx)
    return len([x for x in str(ctx or "").splitlines() if x.strip()])


def shape(question: str) -> str:
    """Question shape from the engine's rule router (no model)."""
    try:
        from memspine.core.query_shape import is_aggregation, is_count, is_inference, is_temporal
    except ImportError:  # run without PYTHONPATH=src: coarse fallback
        q = question.lower()
        if re.search(r"\bhow many\b", q):
            return "count"
        if re.search(r"\b(when|what year|what month)\b", q):
            return "temporal"
        if re.search(r"\b(would|likely|might|could)\b", q):
            return "inference"
        return "other"
    if is_count(question):
        return "count"
    if is_aggregation(question):
        return "list"
    if is_temporal(question):
        return "temporal"
    if is_inference(question):
        return "inference"
    return "other"


def compare(
    base: dict[tuple[str, str], dict[str, Any]], fixed: dict[tuple[str, str], dict[str, Any]]
) -> dict[str, Any]:
    from memspine_evals.refusal import is_refusal

    keys = sorted(set(base) & set(fixed))
    cats: dict[str, Counter[str]] = {}
    lost: list[dict[str, Any]] = []
    gained: list[dict[str, Any]] = []
    for k in keys:
        b, f = base[k], fixed[k]
        c = cats.setdefault(f.get("category") or "?", Counter())
        c["n"] += 1
        bc, fc = bool(b.get("correct")), bool(f.get("correct"))
        if bc and not fc:
            c["lost"] += 1
            lost.append(_summ(b, f, is_refusal))
        elif fc and not bc:
            c["gained"] += 1
            gained.append(_summ(b, f, is_refusal))
    return {"n": len(keys), "categories": cats, "lost": lost, "gained": gained}


def _summ(b: dict[str, Any], f: dict[str, Any], is_refusal: Any) -> dict[str, Any]:
    gold_in = [g.get("in_context") for g in (f.get("gold_turns") or [])]
    return {
        "key": f"{f['item']}/{f['qid']}",
        "category": f.get("category"),
        "shape": shape(f["question"]),
        "question": f["question"],
        "gold": f.get("gold_answer"),
        "base_answer": b.get("answer"),
        "fixed_answer": f.get("answer"),
        "len_ratio": round(_words(f.get("answer", "")) / max(1, _words(b.get("answer", ""))), 2),
        "ctx_lines": (_ctx_lines(b), _ctx_lines(f)),
        "fixed_refusal": bool(is_refusal(f.get("answer", ""))),
        "gold_all_in_context": bool(gold_in) and all(gold_in),
        "outcome": f.get("outcome_class"),
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("base")
    ap.add_argument("fixed")
    ap.add_argument("--runs-dir", default=str(Path(__file__).parent / "runs"))
    ap.add_argument("--items", default=",".join(DEV_ITEMS))
    ap.add_argument("--show", type=int, default=0)
    args = ap.parse_args(argv)
    items = tuple(x for x in args.items.split(",") if x)
    runs = Path(args.runs_dir)
    res = compare(load(runs / f"{args.base}--memspine", items), load(runs / f"{args.fixed}--memspine", items))
    print(f"paired rows: {res['n']}  items: {', '.join(items)}")
    for cat, c in sorted(res["categories"].items()):
        print(f"  {cat:12s} n={c['n']:4d} gained={c['gained']:3d} lost={c['lost']:3d} net={c['gained'] - c['lost']:+d}")
    for name in ("lost", "gained"):
        rows = res[name]
        print(f"\n{name}: {len(rows)}")
        print("  shape:", dict(Counter(r["shape"] for r in rows)))
        if rows:
            short = sum(1 for r in rows if r["len_ratio"] < 0.8)
            print(f"  answer shorter (<0.8x): {short}; fixed refusal: {sum(r['fixed_refusal'] for r in rows)}; "
                  f"gold all in context: {sum(r['gold_all_in_context'] for r in rows)}")
            print("  mean ctx lines base->fixed: "
                  f"{sum(r['ctx_lines'][0] for r in rows) / len(rows):.1f} -> {sum(r['ctx_lines'][1] for r in rows) / len(rows):.1f}")
        for r in rows[: args.show]:
            print(f"   - {r['key']} [{r['shape']}] Q: {r['question']}\n       gold: {r['gold']}\n"
                  f"       base: {r['base_answer']}\n       fixed: {r['fixed_answer']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
