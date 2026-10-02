"""Defect analysis of a C0-1 QA run: where does each wrong answer come from?

For every scored question, joins the run's ``retrieved_ids`` with the dataset's gold evidence
turns and classifies the outcome:

- ``retrieval_miss``: no gold evidence turn reached the context (a deposit/retrieval gap);
- ``partial``: some gold evidence reached the context, not all (multi-evidence questions);
- ``read_fail``: all gold evidence was in context and the answer was still wrong (reader /
  composition / temporal-resolution gap);
- ``correct``.

Usage (from memspine/evals):
    python error_analysis.py --data data/locomo10.json \
        --run ../.venv/runs/c0-locomo-qa-full--memspine
"""

from __future__ import annotations

import argparse
import ast
import json
from collections import Counter, defaultdict
from pathlib import Path

from memspine_evals.datasets import LoCoMoDataset


def load_rows(run: Path) -> list[dict]:
    rows = []
    for line in (run / "results.jsonl").read_text("utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if row.get("kind") == "result" and row.get("status") in ("completed", "truncated"):
            rows.append(row)
    return rows


def classify(row: dict, gold_ids: tuple[str, ...]) -> str:
    correct = float(row["score"]) >= 1.0
    raw = row.get("retrieved_ids") or []
    retrieved = set(raw if isinstance(raw, list) else ast.literal_eval(raw))
    hit = [g for g in gold_ids if g in retrieved]
    if correct:
        return "correct"
    if not gold_ids:
        return "no_gold"
    if not hit:
        return "retrieval_miss"
    if len(hit) < len(gold_ids):
        return "partial"
    return "read_fail"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", required=True, action="append", help="run dir (repeatable)")
    ap.add_argument("--data", required=True)
    ap.add_argument("--examples", type=int, default=0, help="print N read_fail examples per run")
    args = ap.parse_args()
    ds = LoCoMoDataset(args.data, revision_id="auto")
    gold: dict[tuple[str, str], tuple[str, ...]] = {}
    for item in ds.items():
        for q in item.queries:
            gold[(item.item_id, q.query_id)] = tuple(q.gold_turn_ids)
    for run in args.run:
        rows = load_rows(Path(run))
        by_cat: dict[str, Counter] = defaultdict(Counter)
        examples = []
        for row in rows:
            cat = row.get("type_label") or "?"
            outcome = classify(row, gold.get((row["item_id"], row["query_id"]), ()))
            by_cat[cat][outcome] += 1
            by_cat["ALL cat1-4"][outcome] += cat != "cat5"
            if outcome == "read_fail" and len(examples) < args.examples:
                examples.append(row)
        print(f"\n== {Path(run).name}  ({len(rows)} rows)")
        head = ("category", "n", "correct", "ret_miss", "partial", "read_fail")
        print(f"{head[0]:12s} {head[1]:>5s} " + " ".join(f"{h:>9s}" for h in head[2:]))
        for cat in sorted(by_cat):
            c = by_cat[cat]
            n = sum(c.values())
            if not n:
                continue
            print(
                f"{cat:12s} {n:5d} "
                + " ".join(
                    f"{100 * c[k] / n:8.1f}%"
                    for k in ("correct", "retrieval_miss", "partial", "read_fail")
                )
            )
        for row in examples:
            print(f"  [{row['type_label']}] Q: {row['question'][:90]}")
            print(f"     gold: {str(row['gold'])[:60]} | ans: {row['answer'][:100]}")


if __name__ == "__main__":
    main()
