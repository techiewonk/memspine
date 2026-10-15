"""E05 offline coverage on a traced run (no model call, no gold at run time).

The extraction needs a model, so this does not estimate accuracy. It runs the code path that does
not: the question gate, I76's decision (the milestone step only runs where I76 abstained), the dated
lines the extraction prompt would show, the question shape, and the prompt size. The gold is read
only to say how many of the questions the step could still change were wrong before (an upper bound).

    PYTHONPATH=src;evals python evals/milestones_offline.py evals/runs/full-persp-loc--trace/reads.jsonl.gz
"""

from __future__ import annotations

import re
import sys
from collections import Counter

from duration_offline import load
from memspine_evals.duration_solve import solve_duration
from memspine_evals.milestones import (
    _BODY,
    _ordinals_in,
    is_milestone_question,
    numbered_dated_lines,
    render_lines,
)
from memspine_evals.tokens import HeuristicTokenCounter

_SHAPES = (
    ("after_how_many", re.compile(r"\bafter how many\b", re.I)),
    ("how_long_take", re.compile(r"\bhow long (?:did|does|would|will) .*\b(?:take|took)\b", re.I)),
    ("between", re.compile(r"\bbetween\b", re.I)),
    ("since", re.compile(r"\bsince\b", re.I)),
    ("before_after", re.compile(r"\b(?:before|after|until|later)\b", re.I)),
    ("how_long_other", re.compile(r"\bhow long\b", re.I)),
)


def shape(question: str) -> str:
    return next((name for name, rx in _SHAPES if rx.search(question)), "how_many_unit")


def report(rows: list[dict]) -> dict:
    counter = HeuristicTokenCounter()
    out: Counter = Counter()
    shapes: Counter = Counter()
    sizes = []
    eligible_wrong = 0
    for r in rows:
        q = r["question"]
        if not is_milestone_question(q):
            continue
        out["gate_questions"] += 1
        sol, _ = solve_duration(q, r["context_text"], None)
        settled = sol is not None
        out["i76_solved" if settled else "i76_abstained_or_not_duration"] += 1
        if settled:
            continue
        lines = numbered_dated_lines(r["context_text"])
        if not lines:
            out["no_dated_lines"] += 1
            continue
        out["milestone_candidates"] += 1  # the extraction call would be made
        shapes[shape(q)] += 1
        if _ordinals_in(q):
            out["names_an_ordinal"] += 1
        sizes.append(counter.count(_BODY) + counter.count(render_lines(lines)) + counter.count(q))
        if r["verdict"]["score"] < 1:
            eligible_wrong += 1
    out["candidates_wrong_before"] = eligible_wrong
    sizes.sort()
    return {
        "counts": dict(out),
        "shapes": dict(shapes),
        "prompt_tokens": (
            {"median": sizes[len(sizes) // 2], "max": sizes[-1], "total": sum(sizes)}
            if sizes
            else {}
        ),
    }


if __name__ == "__main__":
    rows = load(sys.argv[1])
    print(f"{len(rows)} traced reads")
    res = report(rows)
    for key, val in res.items():
        print(key, val)
