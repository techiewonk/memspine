"""Results table for a run plan: one row per arm/system, merging resume runs.

    python plan_report.py --runs runs --prefix aamas27-locomo-qwen3 [--base memspine-base]

For every ``<prefix>--<arm>[-resume|--rN]--<system>`` run directory it reads
``results.jsonl`` (rows keyed by query_id, a resume row replacing the original),
and prints accuracy, per-category accuracy, mean context tokens, cost (``spend.usd``
of every contributing run) and the delta against ``--base``. Repeats (``--rN``)
are reported as their own rows plus a mean ± sd line per arm.
"""

from __future__ import annotations

import argparse
import json
import re
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

_DIR = re.compile(
    r"^(?P<prefix>.+?)--(?P<arm>.+?)(?P<resume>-resume)?"
    r"(?:--r(?P<rep>\d+))?--(?P<system>[^-].*)$"
)


def _rows(run_dir: Path) -> list[dict[str, Any]]:
    path = run_dir / "results.jsonl"
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        row = json.loads(line)
        if row.get("kind") != "manifest" and "query_id" in row:
            out.append(row)
    return out


def _spend(run_dir: Path) -> float:
    summary = run_dir / "summary.json"
    if not summary.exists():
        return 0.0
    data = json.loads(summary.read_text(encoding="utf-8"))
    return float((data.get("spend") or {}).get("usd") or data["summary"].get("cost_usd") or 0.0)


def collect(runs: Path, prefix: str) -> dict[tuple[str, str, str], dict[str, Any]]:
    groups: dict[tuple[str, str, str], list[Path]] = defaultdict(list)
    for d in sorted(runs.iterdir()):
        if not d.is_dir() or not d.name.startswith(prefix + "--"):
            continue
        m = _DIR.match(d.name)
        if not m or not (d / "results.jsonl").exists():
            continue
        groups[(m["arm"], m["system"], m["rep"] or "")].append(d)
    table: dict[tuple[str, str, str], dict[str, Any]] = {}
    for key, dirs in groups.items():
        merged: dict[str, dict[str, Any]] = {}
        # originals first, resumes last, so a resume row replaces the original's
        for d in sorted(dirs, key=lambda p: "-resume--" in p.name):
            for row in _rows(d):
                merged[row["query_id"]] = row
        by: dict[str, list[float]] = defaultdict(list)
        ctx = 0
        for row in merged.values():
            ok = row.get("status") in ("completed", "truncated") and row.get("score") is not None
            by[row.get("type_label") or "?"].append(float(row["score"]) if ok else 0.0)
            ctx += int(row.get("context_tokens") or 0)
        values = [v for vs in by.values() for v in vs]
        # a crashed run has no summary, but its resume completes the arm: judge by rows
        complete = any((d / "summary.json").exists() for d in dirs)
        table[key] = {
            "n": len(merged),
            "acc": 100 * sum(values) / max(1, len(values)),
            "by": {k: 100 * sum(v) / len(v) for k, v in sorted(by.items())},
            "ctx": ctx // max(1, len(merged)),
            "usd": sum(_spend(d) for d in dirs),
            "complete": complete,
        }
    return table


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--runs", default=str(Path(__file__).resolve().parent / "runs"))
    ap.add_argument("--prefix", required=True)
    ap.add_argument("--base", default="memspine-base")
    ap.add_argument("--n", type=int, default=1540, help="expected questions per arm")
    args = ap.parse_args(argv)
    table = collect(Path(args.runs), args.prefix)
    base = next((v for (arm, _s, rep), v in table.items() if arm == args.base and not rep), None)
    cats = sorted({c for v in table.values() for c in v["by"]})
    head = ["arm", "system", "acc", "Δ base", *cats, "ctx", "$", "n"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for (arm, system, rep), v in sorted(table.items(), key=lambda kv: -kv[1]["acc"]):
        delta = f"{v['acc'] - base['acc']:+.1f}" if base and system == "memspine" else ""
        flag = "" if v["complete"] and v["n"] == args.n else " (partial)"
        name = arm + (f" r{rep}" if rep else "")
        by_cat = (f"{v['by'].get(c, 0):.1f}" for c in cats)
        cells = [name + flag, system, f"{v['acc']:.1f}", delta, *by_cat,
                 str(v["ctx"]), f"{v['usd']:.2f}", str(v["n"])]  # fmt: skip
        print("| " + " | ".join(cells) + " |")
    reps: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (arm, system, _rep), v in table.items():
        if v["complete"] and v["n"] == args.n:
            reps[(arm, system)].append(v["acc"])
    multi = {k: v for k, v in reps.items() if len(v) > 1}
    if multi:
        print()
        for (arm, system), accs in sorted(multi.items()):
            mean, sd = statistics.mean(accs), statistics.stdev(accs)
            print(f"- {arm} / {system}: mean {mean:.2f} ± {sd:.2f} "
                  f"over {len(accs)} runs ({', '.join(f'{a:.1f}' for a in accs)})")  # fmt: skip
    total = sum(v["usd"] for v in table.values())
    print(f"\nTotal metered spend for these runs: ${total:.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
