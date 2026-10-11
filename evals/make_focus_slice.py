"""Build the focus slices used by the fast screens (FAIL set + seeded control sample).

    python make_focus_slice.py [--runs-dir DIR]

LoCoMo  -> analysis/focus_slice_r7protect.json : every question wrong in r7-protect-full (cat 1-4) plus a seeded
           (20261011) sample of 150 correct ones, stratified by category x conversation, proportional.
           Correct = score_date_checked/score >= 1 and status completed and non-empty answer (eval_screen.py rule).
OP-Bench -> analysis/focus_slice_opb.json     : probes with final per_probe score < 0.5 in r7-protect-opb
           (opbench_summary.json, the finalised M01 scores) plus 60 seeded probes >= 0.5.

Ids are "<item_id>/<query_id>" so `--query-ids @file` (cli.py c0-1) can select them; the file's `query_ids` is
FAIL + CONTROL in dataset order. The `hash` is the sha256 of the sorted labelled ids.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SEED = 20261011


def locomo_correct(d: dict) -> bool:
    sc = d.get("score_date_checked", d.get("score"))
    return float(sc or 0) >= 1 and d["status"] == "completed" and bool((d.get("answer") or "").strip())


def read_results(path: Path) -> list[dict]:
    rows = []
    for line in path.open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") == "result":
            rows.append(d)
    return rows


def stratified_sample(strata: dict[tuple, list[str]], n: int, rng: random.Random) -> list[str]:
    """Proportional allocation (largest remainder) of n over the strata, then a seeded draw in each."""
    total = sum(len(v) for v in strata.values())
    keys = sorted(strata)
    quota = {k: n * len(strata[k]) / total for k in keys}
    alloc = {k: int(quota[k]) for k in keys}
    rest = n - sum(alloc.values())
    for k in sorted(keys, key=lambda k: (-(quota[k] - alloc[k]), k))[:rest]:
        alloc[k] += 1
    out: list[str] = []
    for k in keys:
        pool = sorted(strata[k])
        out += rng.sample(pool, min(alloc[k], len(pool)))
    return out


def digest(fail: list[str], control: list[str]) -> str:
    blob = json.dumps({"fail": sorted(fail), "control": sorted(control)}, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def build_locomo(runs: Path, ref: str, n_control: int) -> dict:
    rows = read_results(runs / f"{ref}--memspine" / "results.jsonl")
    order = [f"{d['item_id']}/{d['query_id']}" for d in rows]
    fail, strata = [], defaultdict(list)
    for d in rows:
        qid = f"{d['item_id']}/{d['query_id']}"
        if locomo_correct(d):
            strata[(d["type_label"], d["item_id"])].append(qid)
        else:
            fail.append(qid)
    control = stratified_sample(strata, n_control, random.Random(SEED))
    sel = set(fail) | set(control)
    n_correct = sum(len(v) for v in strata.values())
    cat = {f"{d['item_id']}/{d['query_id']}": d["type_label"] for d in rows}
    return {
        "benchmark": "locomo",
        "source_run": ref,
        "seed": SEED,
        "stratify": "category x conversation, proportional",
        "n_total": len(rows),
        "n_correct_total": n_correct,
        "sizes": {"fail": len(fail), "control": len(control), "total": len(sel)},
        "hash": digest(fail, control),
        "fail": fail,
        "control": control,
        "category": {q: cat[q] for q in sorted(sel)},
        "query_ids": [q for q in order if q in sel],
    }


def build_opb(runs: Path, ref: str, n_control: int) -> dict:
    run = runs / f"{ref}--memspine"
    per = json.loads((run / "opbench_summary.json").read_text(encoding="utf-8"))["per_probe"]
    item_of = {d["query_id"]: d["item_id"] for d in read_results(run / "results.jsonl")}
    fail = [f"{item_of[q]}/{q}" for q, s in per.items() if s < 0.5]
    ok = [f"{item_of[q]}/{q}" for q, s in per.items() if s >= 0.5]
    control = sorted(random.Random(SEED).sample(sorted(ok), min(n_control, len(ok))))
    sel = set(fail) | set(control)
    order = [f"{item_of[q]}/{q}" for q in per]
    return {
        "benchmark": "op_bench",
        "source_run": ref,
        "seed": SEED,
        "threshold": 0.5,
        "n_total": len(per),
        "n_ok_total": len(ok),
        "sizes": {"fail": len(fail), "control": len(control), "total": len(sel)},
        "hash": digest(fail, control),
        "fail": fail,
        "control": control,
        "baseline_score": {f"{item_of[q]}/{q}": s for q, s in per.items() if f"{item_of[q]}/{q}" in sel},
        "query_ids": [q for q in order if q in sel],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    ap.add_argument("--loc-ref", default="r7-protect-full")
    ap.add_argument("--opb-ref", default="r7-protect-opb")
    ap.add_argument("--out-dir", type=Path, default=HERE / "analysis")
    a = ap.parse_args()
    for name, obj in (
        ("focus_slice_r7protect.json", build_locomo(a.runs_dir, a.loc_ref, 150)),
        ("focus_slice_opb.json", build_opb(a.runs_dir, a.opb_ref, 60)),
    ):
        (a.out_dir / name).write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8")
        print(name, obj["sizes"], obj["hash"][:12])


if __name__ == "__main__":
    main()
