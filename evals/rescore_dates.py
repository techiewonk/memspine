"""Apply the deterministic date-equivalence check (gap A2) to stored QA runs, offline.

    python evals/rescore_dates.py runs/<run>--memspine [more runs ...]

No model calls. For every completed row judged wrong whose gold and answer name the same single
day (`memspine_evals.date_check.date_equivalent`), the verdict is flipped to correct. Prints, per
run, the original and rescored accuracy over all result rows, and lists each flipped question.
Writes nothing unless --write, which adds `date_check` and `score_date_checked` to each row of
results.jsonl (the original `score` is kept).
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE.resolve()]
sys.path.append(str(HERE))

from memspine_evals.date_check import date_equivalent  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    for run in args.runs:
        path = Path(run) / "results.jsonl"
        lines = path.read_text(encoding="utf-8").splitlines()
        rows = [json.loads(line) for line in lines if line.strip()]
        res = [r for r in rows if r.get("kind") == "result"]
        before = sum(1 for r in res if float(r.get("score") or 0) >= 1)
        flipped = []
        for r in rows:
            if r.get("kind") != "result":
                continue
            ok = float(r.get("score") or 0) >= 1
            hit = (
                (not ok)
                and r.get("status") == "completed"
                and date_equivalent(r.get("gold"), r.get("answer"))
            )
            if hit:
                flipped.append(r)
            if args.write:
                r["date_check"] = bool(hit)
                r["score_date_checked"] = 1.0 if (ok or hit) else 0.0
        n = len(res)
        after = 100 * (before + len(flipped)) / n
        print(
            f"{Path(run).name}: {100 * before / n:.1f}% -> {after:.1f}% "
            f"({len(flipped)} of {n} flipped)"
        )
        for r in flipped:
            ans = r["answer"][:90]
            print(f"   {r['item_id']}/{r['query_id']}: gold {r['gold']!r} | answer {ans!r}")
        if args.write:
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
