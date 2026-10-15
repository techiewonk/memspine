"""G-C7: add the official LoCoMo QA F1 to any finished run, offline (no model call).

    python evals/rescore_f1.py runs/<run>--memspine [more runs ...] [--write] [--runs-dir evals/runs]

A run argument may be a results directory, or a bare run id (resolved as <runs-dir>/<id>--memspine). Per run it
prints the judge accuracy (the unchanged `score` column) and the mean official F1 (x100) per category and over
categories 1-4, from the saved `answer` and `gold` (scorer: memspine_evals/locomo_f1.py, an independent port of
snap-research/locomo task_eval/evaluation.py; see that module's licence note). `--write` adds `f1_locomo` (0..1) to
each result row of results.jsonl; `score` is kept. Non-completed rows score 0 and stay in the denominator.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE.resolve()]
sys.path.append(str(HERE))

from memspine_evals.locomo_f1 import CAT_NAMES, category_of, row_f1, summarise


def rescore(path: Path, write: bool) -> dict:
    rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
    by: dict[int, list[float]] = {}
    judge: dict[int, list[float]] = {}
    for r in rows:
        if r.get("kind") != "result":
            continue
        f1 = row_f1(r)
        if f1 is None:
            continue
        c = category_of(r["type_label"])
        by.setdefault(c, []).append(f1)
        ok = (
            float(r.get("score") or 0) >= 1
            and r.get("status") == "completed"
            and bool((r.get("answer") or "").strip())
        )
        judge.setdefault(c, []).append(1.0 if ok else 0.0)
        if write:
            r["f1_locomo"] = f1
    if write:
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    out = summarise(by)
    j14 = [x for c, v in judge.items() if c != 5 for x in v]
    out["judge_1_4"] = 100 * sum(j14) / len(j14) if j14 else None
    for c, v in judge.items():
        out["by_category"][c]["judge"] = 100 * sum(v) / len(v)
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    a = ap.parse_args()
    for run in a.runs:
        d = Path(run)
        if not (d / "results.jsonl").exists():
            d = a.runs_dir / f"{run.removesuffix('--memspine')}--memspine"
        s = rescore(d / "results.jsonl", a.write)
        print(
            f"{d.name}: categories 1-4 n={s['n_1_4']}  judge {s['judge_1_4']:.1f}  F1 (official LoCoMo) {s['f1_1_4']:.1f}"
        )
        for c, v in s["by_category"].items():
            tag = " (abstention check, not in the 1-4 headline)" if c == 5 else ""
            print(
                f"   cat{c} {CAT_NAMES[c]:11s} n={v['n']:4d}  judge {v['judge']:5.1f}  F1 {v['f1']:5.1f}{tag}"
            )


if __name__ == "__main__":
    main()
