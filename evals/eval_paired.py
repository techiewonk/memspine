"""G-E1: paired verdict of an arm against a named reference over ALL questions of a full run.

    python eval_paired.py ARM_RUN --ref REF_RUN [--runs-dir runs]

Both runs are read from ``<runs>/<id>--memspine/results.jsonl``. The expected question set is the union of both
runs, so a question missing from either makes the verdict INCOMPLETE. Correctness uses the date-checked score when
present (as ``eval_focus.py``); categories come from each row's ``type_label``. Same gate as the focus scorer
(``memspine_evals.paired_gate``). Prints counts only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from memspine_evals.paired_gate import format_result, paired

HERE = Path(__file__).parent


def correct(d: dict) -> bool:
    sc = d.get("score_date_checked", d.get("score"))
    return float(sc or 0) >= 1 and d.get("status") == "completed" and bool((d.get("answer") or "").strip())


def load(runs: Path, run: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for line in (runs / f"{run}--memspine" / "results.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") == "result":
            out[f"{d['item_id']}/{d['query_id']}"] = d
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arm")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    a = ap.parse_args(argv)
    if a.ref == a.arm:
        ap.error("--ref must name a different run than the arm")
    arm, ref = load(a.runs_dir, a.arm), load(a.runs_dir, a.ref)
    label = {q: d.get("type_label", "?") for q, d in {**ref, **arm}.items()}
    r = paired({q: correct(d) for q, d in arm.items()}, {q: correct(d) for q, d in ref.items()},
               sorted(set(arm) | set(ref)), category_of=label.__getitem__)
    for line in format_result(f"{a.arm} vs {a.ref}", r):
        print(line)
    print(f"verdict: {r.verdict}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
