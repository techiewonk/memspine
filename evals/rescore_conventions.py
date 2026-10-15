"""I58: score finished QA runs under the judge conventions, offline (two columns).

    python evals/rescore_conventions.py runs/<run>--memspine [more runs ...] [--write]

No model call. For every completed binary row the LLM judge called wrong, the deterministic
convention layer (`memspine_evals.judge_conventions`: numeral, typo, list superset) is asked
whether the answer is right under it. Per run it prints the judge-only accuracy (the unchanged
`score` column), the accuracy under the conventions, and each credited question with its rule, so
every credit can be read by hand. Abstention rows (gold "Not mentioned ...") are never touched.
`--write` adds `score_conventions` and `convention` to each row of results.jsonl (`score` is kept),
the same two-column shape `--judge-conventions` records at run time.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE.resolve()]
sys.path.append(str(HERE))

from memspine_evals.judge_conventions import check_conventions, check_conventions_v2  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("runs", nargs="+")
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()
    for run in args.runs:
        path = Path(run) / "results.jsonl"
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        res = [r for r in rows if r.get("kind") == "result"]
        before = sum(1 for r in res if float(r.get("score") or 0) >= 1)
        credited = []
        credited2 = []
        for r in res:
            ok = float(r.get("score") or 0) >= 1
            hit = hit2 = None
            if not ok and r.get("status") == "completed" and r.get("scale") == "binary":
                q, a, g = r.get("question") or "", r.get("answer") or "", r.get("gold")
                hit = check_conventions(q, a, g)
                hit2 = check_conventions_v2(q, a, g)  # I77: v1 + date format, unit, ordinal, alias
            if hit2 is not None:
                credited2.append((r, hit2))
            if hit is not None:
                credited.append((r, hit))
            if args.write:
                r["score_conventions"] = 1.0 if (ok or hit is not None) else 0.0
                if hit is not None:
                    r["convention"] = hit.rule
                r["score_conventions_v2"] = 1.0 if (ok or hit2 is not None) else 0.0
                if hit2 is not None:
                    r["convention_v2"] = hit2.rule
        n = len(res)
        print(
            f"{Path(run).name}: judge {100 * before / n:.1f}% | judge+conventions "
            f"{100 * (before + len(credited)) / n:.1f}% ({len(credited)} of {n} credited)"
        )
        for r, hit in credited:
            print(f"   {r['item_id']}/{r['query_id']} [{hit.rule}]: gold {r['gold']!r} | answer {r['answer'][:90]!r}")
        print(
            f"   v2 (third column): {100 * (before + len(credited2)) / n:.1f}% "
            f"({len(credited2)} credited, {len(credited2) - len(credited)} more than v1)"
        )
        v1_ids = {(r["item_id"], r["query_id"]) for r, _ in credited}
        for r, hit in credited2:
            if (r["item_id"], r["query_id"]) not in v1_ids:
                print(f"   NEW {r['item_id']}/{r['query_id']} [{hit.rule}]: gold {r['gold']!r} | answer {r['answer'][:90]!r}")
        if args.write:
            path.write_text("\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
