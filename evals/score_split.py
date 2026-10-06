"""Score arms whose runs were split one conversation per run (``<run-id>--<conv>``).

    python score_split.py --plan-id aamas27-locomo-qwen3 --arms w1-combo-A,w1-dated3

For each arm: accuracy over all completed rows (errors count 0), per LoCoMo
category, rows, conversations present / missing, and the summed cost from each
run's ``summary.json``. Rows are de-duplicated by (item_id, query_id): a later
run's row (a retry) replaces an earlier one.
"""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path

RUNS = Path(__file__).resolve().parent / "runs"
CONVS = ["conv-26", "conv-30", "conv-41", "conv-42", "conv-43", "conv-44", "conv-47",
         "conv-48", "conv-49", "conv-50"]  # fmt: skip


def arm_rows(plan_id: str, arm: str) -> tuple[dict[tuple[str, str], dict], set[str], float]:
    rows: dict[tuple[str, str], dict] = {}
    convs: set[str] = set()
    cost = 0.0
    for run_dir in sorted(
        RUNS.glob(f"{plan_id}--{arm}--conv-*--memspine"), key=lambda p: p.stat().st_mtime
    ):
        results = run_dir / "results.jsonl"
        summary_seen = False
        if not results.exists():
            continue
        for line in results.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            if row.get("kind") == "summary":
                summary_seen = True
            if row.get("kind") != "result":
                continue
            rows[(row["item_id"], row["query_id"])] = row
        if summary_seen:
            convs.add(run_dir.name.split("--")[-2])
        summary = run_dir / "summary.json"
        if summary.exists():
            data = json.loads(summary.read_text(encoding="utf-8"))
            cost += float((data.get("spend") or {}).get("usd") or 0.0)
    return rows, convs, cost


def score(rows: dict[tuple[str, str], dict]) -> dict[str, tuple[float, int]]:
    by: dict[str, list[float]] = defaultdict(list)
    for row in rows.values():
        value = float(row.get("score") or 0.0) if row.get("status") == "completed" else 0.0
        by["all"].append(value)
        by[str(row.get("type_label"))].append(value)
    return {k: (100.0 * sum(v) / len(v), len(v)) for k, v in by.items()}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan-id", default="aamas27-locomo-qwen3")
    ap.add_argument("--arms", required=True)
    args = ap.parse_args()
    cats = ["cat1", "cat2", "cat3", "cat4", "cat5"]
    print("| arm | overall | " + " | ".join(cats) + " | rows | convs | missing | $ |")
    print("|---|---|" + "---|" * len(cats) + "---|---|---|---|")
    for arm in args.arms.split(","):
        rows, convs, cost = arm_rows(args.plan_id, arm)
        s = score(rows)
        cells = [f"{s[c][0]:.1f}" if c in s else "-" for c in cats]
        missing = ",".join(c for c in CONVS if c not in convs) or "-"
        overall = f"{s['all'][0]:.2f}" if "all" in s else "-"
        print(
            f"| {arm} | {overall} | "
            + " | ".join(cells)
            + f" | {s.get('all', (0, 0))[1]} | {len(convs)} | {missing} | {cost:.2f} |"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
