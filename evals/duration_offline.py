"""I76 / I79 offline estimate on a traced run (no model call, no gold at run time).

Replays the duration solver (``memspine_evals.duration_solve``, rewrite mode) and the I79
annotator over the traced reader contexts and answers of a full run
(``evals/runs/<run>--trace/reads.jsonl[.gz]``), and counts, against the gold, what the post-step
would have fixed or broken. The gold is used here, to score the estimate, never by the solver.

    PYTHONPATH=src:evals python evals/duration_offline.py evals/runs/full-persp-loc--trace/reads.jsonl.gz
"""

from __future__ import annotations

import gzip
import json
import re
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from memspine_evals.date_repair import _abs_dates
from memspine_evals.duration_solve import (
    _UNIT_DAYS,
    check_answer,
    reader_days,
    solve_duration,
)

from memspine.core.temporal_resolve import annotate

_ANNOTATION = re.compile(r"\s*\[=[^\]]*\]")
_LINE = re.compile(
    r"^(\s*(?:\*\s*|\[hit \d+\]\s*)?)\[(\d{4}-\d{2}-\d{2})(?: [A-Za-z]{3})?\]\s*(.*)$"
)


def load(path: str) -> list[dict]:
    p = Path(path)
    op = gzip.open if p.suffix == ".gz" else open
    with op(p, "rt", encoding="utf8") as fh:
        return [json.loads(line) for line in fh]


def gold_days(gold: str) -> float | None:
    return reader_days(gold, "")


def duration_estimate(rows: list[dict]) -> dict:
    out = Counter()
    detail = []
    for r in rows:
        sol, meta = solve_duration(r["question"], r["context_text"], None)
        if not meta:
            continue
        was_ok = r["verdict"]["score"] >= 1
        gd = gold_days(r["gold"] or "")
        out["duration_questions"] += 1
        out["duration_wrong_before" if not was_ok else "duration_right_before"] += 1
        if sol is None:
            out["abstain:" + meta["decision"].split(":", 1)[-1]] += 1
            continue
        verdict, _ = check_answer(sol, r["answer"])
        match = gd is not None and int(gd / _UNIT_DAYS[sol.unit] + 0.5) == sol.n
        out["solved"] += 1
        changed = verdict in ("disagrees", "refusal")
        if not changed:
            out["solved_but_reader_" + verdict] += 1
        elif not was_ok and match:
            out["FIXED"] += 1
        elif was_ok and not match:
            out["BROKEN"] += 1
        elif was_ok and match:
            out["rewritten_right_to_right"] += 1
        else:
            out["rewritten_wrong_to_wrong"] += 1
        detail.append(
            (r["item_id"], r["query_id"], r["question"], r["gold"], r["answer"][:60],
             sol.text(), was_ok, match, verdict)
        )  # fmt: skip
    return {"counts": dict(out), "detail": detail}


def _gold_dates(gold: str):
    fixed = re.sub(r"(\d)([A-Za-z])", r"\1 \2", gold or "")
    return [d for _, _, d in _abs_dates(fixed)]


def annotator_estimate(rows: list[dict]) -> dict:
    """New bare-weekday annotations the I79 annotator adds to the traced contexts."""
    out = Counter()
    detail = []
    for r in rows:
        if r["type_label"] != "cat2":
            continue
        was_ok = r["verdict"]["score"] >= 1
        gold_ds = set(_gold_dates(r["gold"]))
        new_lines = []
        for raw in r["context_text"].split("\n"):
            m = _LINE.match(raw)
            if not m:
                continue
            said = datetime.strptime(m.group(2), "%Y-%m-%d").date()
            bare = _ANNOTATION.sub("", m.group(3))
            kw = dict(anchored=True, durations=True)
            before = annotate(bare, said, **kw)
            after = annotate(bare, said, weekdays=True, **kw)
            if before != after:
                new_lines.append(after)
        if new_lines:
            out["questions_with_new_annotation"] += 1
            out["new_annotation_on_right" if was_ok else "new_annotation_on_wrong"] += 1
            out["lines_added"] += len(new_lines)
            hit = [
                ln for ln in new_lines
                if any(g.isoformat() in ln or f"{g:%Y-%m-%d}" in ln for g in gold_ds)
            ]  # fmt: skip
            if hit and not was_ok:
                out["wrong_with_gold_date_now_in_prompt"] += 1
                detail.append((r["item_id"], r["query_id"], r["question"], r["gold"], hit[0][:200]))
    return {"counts": dict(out), "detail": detail}


def main() -> None:
    rows = load(sys.argv[1])
    cat2 = [r for r in rows if r["type_label"] == "cat2"]
    print(f"{len(rows)} questions, {len(cat2)} temporal, "
          f"{sum(r['verdict']['score'] < 1 for r in cat2)} temporal wrong")  # fmt: skip
    for label, subset in (("temporal (cat2)", cat2), ("all categories", rows)):
        est = duration_estimate(subset)
        print(f"\n== I76 duration solver (rewrite mode) on {label}")
        for k, v in sorted(est["counts"].items()):
            print(f"  {k}: {v}")
        if label.startswith("all"):
            for d in est["detail"]:
                print("  ", d)
    est = annotator_estimate(rows)
    print("\n== I79 bare-weekday annotator on temporal contexts")
    for k, v in sorted(est["counts"].items()):
        print(f"  {k}: {v}")
    for d in est["detail"]:
        print("  ", d)


if __name__ == "__main__":
    main()
