"""R14-M-1 offline check: list-mode trigger recall / false fire, old vs new, on forensics.

CPU only. Inputs (local, not committed): the reference full run's ``forensics.jsonl`` (query,
vector leg scores per question) and ``evals/analysis/r14_retrieval_features.csv`` (qid, n_gold,
list_mode = the old trigger's decision in that run). Output: counts and question ids only,
never question text. Run from the repo root with the memspine .venv python:

    python evals/analysis/r14_listcov_trigger_check.py <forensics.jsonl> [--cv] [--ids-out f.json]

"old" = the ``list_mode`` column (the set_question regex as run). "new" = ``list_signal`` with
the shipped weights on the first 30 vector scores. "union" = old or new. ``--cv`` refits the
same logistic form with sklearn by conversation (GroupKFold 5) and reports recall at the
false-fire level of the shipped threshold, as an out-of-sample estimate of the fit.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

WT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(WT / "src"))

from memspine.read.list_trigger import list_signal  # noqa: E402

CSV = WT / "evals/analysis/r14_retrieval_features.csv"
DEPTH = 30


def rate(fired: dict[str, bool], ids: list[str]) -> tuple[int, int]:
    return sum(fired[q] for q in ids), len(ids)


def fmt(name: str, f3: tuple[int, int], f1: tuple[int, int]) -> str:
    return (
        f"{name:6s} recall(n_gold>=3) {f3[0]}/{f3[1]} = {f3[0] / f3[1]:.1%}   "
        f"false-fire(n_gold==1) {f1[0]}/{f1[1]} = {f1[0] / f1[1]:.1%}"
    )


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("forensics")
    ap.add_argument("--cv", action="store_true")
    ap.add_argument("--ids-out")
    a = ap.parse_args()
    with open(CSV, encoding="utf8") as fh:
        rows = {r["qid"]: r for r in csv.DictReader(fh)}
    fx = {}
    with open(a.forensics, encoding="utf8") as fh:
        for line in fh:
            d = json.loads(line)
            if d["query_id"] in rows:
                fx[d["query_id"]] = d
    g3 = [q for q, r in rows.items() if int(r["n_gold"]) >= 3 and q in fx]
    g1 = [q for q, r in rows.items() if int(r["n_gold"]) == 1 and q in fx]
    old = {q: rows[q]["list_mode"] == "1" for q in fx}
    sig = {
        q: list_signal(fx[q]["query"], [h["score"] for h in fx[q]["vector"][:DEPTH]]) for q in fx
    }
    new = {q: sig[q].fire for q in fx}
    union = {q: old[q] or new[q] for q in fx}
    print(f"questions {len(fx)}; n_gold>=3: {len(g3)}; n_gold==1: {len(g1)}")
    for name, m in (("old", old), ("new", new), ("union", union)):
        print(fmt(name, rate(m, g3), rate(m, g1)))
    n2 = [q for q, r in rows.items() if int(r["n_gold"]) == 2 and q in fx]
    print(f"n_gold==2 fire: old {rate(old, n2)[0]}/{len(n2)}, new {rate(new, n2)[0]}/{len(n2)}")
    if a.cv:
        import numpy as np
        from sklearn.linear_model import LogisticRegression
        from sklearn.model_selection import GroupKFold

        qs = g3 + g1
        y = np.array([1] * len(g3) + [0] * len(g1))
        names = list(next(iter(sig.values())).features)
        x = np.array([[sig[q].features[k] for k in names] for q in qs], float)
        grp = [rows[q]["item"] for q in qs]
        pr = np.zeros(len(y))
        for tr, te in GroupKFold(5).split(x, y, grp):
            mu, sd = x[tr].mean(0), x[tr].std(0) + 1e-9
            lr = LogisticRegression(max_iter=5000, class_weight="balanced")
            m = lr.fit((x[tr] - mu) / sd, y[tr])
            pr[te] = m.predict_proba((x[te] - mu) / sd)[:, 1]
        fp = sum(new[q] for q in g1) / len(g1)
        th = np.percentile(pr[y == 0], 100 * (1 - fp))
        rec = (pr[y == 1] >= th).mean()
        print(f"cv(5 folds by conversation) recall at false-fire {fp:.1%}: {rec:.1%}")
    if a.ids_out:
        out = {
            "n_gold_ge3_missed_new": [q for q in g3 if not new[q]],
            "n_gold_1_false_fire_new": [q for q in g1 if new[q]],
            "n_gold_ge3_missed_old": [q for q in g3 if not old[q]],
        }
        Path(a.ids_out).write_text(json.dumps(out, indent=1), encoding="utf8")


if __name__ == "__main__":
    main()
