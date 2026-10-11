"""Score a focus-slice screen run (FAIL questions + CONTROL sample) against the reference run.

    python eval_focus.py ARM_RUN [--ref r7-protect-full] [--slice analysis/focus_slice_r7protect.json]
    python eval_focus.py ARM_OPB_RUN --opb [--slice analysis/focus_slice_opb.json]

LoCoMo (G-E1): the verdict is PAIRED against the reference run named by --ref, question by question over the slice
  (``memspine_evals.paired_gate``): INCOMPLETE if either run lacks a slice question, NO CHANGE if nothing flips,
  PROMOTE only if net > 0.328*sqrt(n), exact sign test p < 0.05, conversation-cluster bootstrap low > 0 and no
  category beyond its own band. --ref is REQUIRED and must differ from the arm. The old slice-label view (fixed FAIL /
  broken CONTROL vs the labels stored in the slice) is still printed, marked informational: it is not a verdict
  (it printed PROMOTE for an arm identical to its reference on 2026-10-11).
OP-Bench (auto when the run has opbench_summary.json): mean per-probe score change on FAIL and on CONTROL (finalised
  M01 scores vs the slice's baseline_score); est. overall change in points = (sum FAIL delta + mean CONTROL delta *
  n_ok_total) / n_total * 100; PROMOTE iff > +3 pts band and CONTROL mean change >= -0.03.
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

from memspine_evals.paired_gate import format_result, paired

HERE = Path(__file__).parent
BROKEN_MAX = 0.03
OPB_BAND = 3.0


def correct(d: dict) -> bool:
    sc = d.get("score_date_checked", d.get("score"))
    return float(sc or 0) >= 1 and d["status"] == "completed" and bool((d.get("answer") or "").strip())


def read_rows(path: Path) -> dict[str, dict]:
    out = {}
    for line in path.open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") == "result":
            out[f"{d['item_id']}/{d['query_id']}"] = d
    return out


def score_locomo(arm: dict, sl: dict) -> dict:
    fail, control = sl["fail"], sl["control"]
    cat = sl["category"]
    miss_f = [q for q in fail if q not in arm]
    miss_c = [q for q in control if q not in arm]
    fixed = [q for q in fail if q in arm and correct(arm[q])]
    broken = [q for q in control if q in arm and not correct(arm[q])]
    n_c = len(control) - len(miss_c)
    scale = sl["n_correct_total"] / max(len(control), 1)
    net = len(fixed) - len(broken) * scale
    band = 0.328 * math.sqrt(sl["n_total"])
    rate = len(broken) / max(n_c, 1)
    by = defaultdict(lambda: [0, 0, 0, 0])  # fail n, fixed, control n, broken
    for q in fail:
        by[cat[q]][0] += 1
        by[cat[q]][1] += q in fixed
    for q in control:
        by[cat[q]][2] += 1
        by[cat[q]][3] += q in broken
    verdict = "PROMOTE" if net > band and rate <= BROKEN_MAX else ("HOLD" if net > 0 else "REJECT")
    return dict(fixed=len(fixed), broken=len(broken), n_fail=len(fail), n_control=len(control), scale=scale, net=net,
                band=band, broken_rate=rate, by=dict(by), missing=len(miss_f) + len(miss_c), verdict=verdict)


def f1_summary(rows: dict[str, dict], qids: list[str], cat: dict[str, str]) -> dict[str, tuple[int, float]]:
    """G-C7: official LoCoMo F1 (x100) over the slice questions present in `rows`: overall (cat 1-4) and per category."""
    from memspine_evals.locomo_f1 import row_f1

    by: dict[str, list[float]] = defaultdict(list)
    for q in qids:
        if q in rows and cat.get(q) != "cat5" and (f := row_f1(rows[q])) is not None:
            by[cat[q]].append(f)
            by["all"].append(f)
    return {k: (len(v), 100 * sum(v) / len(v)) for k, v in sorted(by.items())}


def score_opb(run_dir: Path, sl: dict) -> dict:
    per = json.loads((run_dir / "opbench_summary.json").read_text(encoding="utf-8"))["per_probe"]
    item_of = {f"{d['item_id']}/{d['query_id']}": q for q, d in
               ((d["query_id"], d) for d in read_rows(run_dir / "results.jsonl").values())}
    base = sl["baseline_score"]
    new = {k: per[q] for k, q in item_of.items() if q in per}
    out = {}
    for name in ("fail", "control"):
        ids = [k for k in sl[name] if k in new]
        out[name] = (len(ids), sum(new[k] - base[k] for k in ids))
    n_f, s_f = out["fail"]
    n_c, s_c = out["control"]
    mean_c = s_c / n_c if n_c else 0.0
    est = (s_f + mean_c * sl["n_ok_total"]) / sl["n_total"] * 100
    verdict = "PROMOTE" if est > OPB_BAND and mean_c >= -0.03 else ("HOLD" if est > 0 else "REJECT")
    return dict(n_fail=n_f, mean_fail=s_f / n_f if n_f else 0.0, n_control=n_c, mean_control=mean_c, est_points=est,
                band=OPB_BAND, missing=len(sl["fail"]) + len(sl["control"]) - n_f - n_c, verdict=verdict)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("arm_run")
    ap.add_argument("--ref", default=None, help="reference run id (required for LoCoMo; the verdict is paired against it)")
    ap.add_argument("--slice", type=Path, default=None)
    ap.add_argument("--opb", action="store_true", help="force OP-Bench scoring (auto if opbench_summary.json exists)")
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    a = ap.parse_args()
    run_dir = a.runs_dir / f"{a.arm_run}--memspine"
    is_opb = a.opb or (run_dir / "opbench_summary.json").exists()
    sl_path = a.slice or HERE / "analysis" / ("focus_slice_opb.json" if is_opb else "focus_slice_r7protect.json")
    sl = json.loads(sl_path.read_text(encoding="utf-8"))
    if not is_opb:
        if not a.ref:
            ap.error("--ref is required: the verdict is paired against a named reference run (G-E1)")
        if a.ref == a.arm_run:
            ap.error("--ref must name a different run than the arm")
        if a.ref != sl["source_run"]:
            print(f"note: slice was built from {sl['source_run']}; the verdict is paired against {a.ref}")
    if is_opb:
        r = score_opb(run_dir, sl)
        print(f"OP-Bench {a.arm_run} vs {sl['source_run']} (slice {sl_path.name} {sl['hash'][:10]})")
        print(f"   FAIL    n={r['n_fail']:3d} mean score change {r['mean_fail']:+.3f}")
        print(f"   CONTROL n={r['n_control']:3d} mean score change {r['mean_control']:+.3f}")
        print(f"   est. full-set change {r['est_points']:+.2f} pts (band +-{r['band']}); missing {r['missing']}")
        print(f"verdict: {r['verdict']}")
        return
    arm = read_rows(run_dir / "results.jsonl")
    r = score_locomo(arm, sl)
    print(f"LoCoMo {a.arm_run} vs {a.ref} (slice {sl_path.name} {sl['hash'][:10]})")
    print(f"   slice view vs stored labels (informational, not a verdict; built from {sl['source_run']}):")
    print(f"   fixed  {r['fixed']}/{r['n_fail']} FAIL questions now correct")
    print(f"   broken {r['broken']}/{r['n_control']} CONTROL questions now wrong ({100*r['broken_rate']:.1f}%, max {100*BROKEN_MAX:.0f}%)")
    print(f"   est. full-set net {r['net']:+.1f} q = fixed - broken*{r['scale']:.2f}  (band +-{r['band']:.1f} = 0.328*sqrt({sl['n_total']}))")
    for c in sorted(r["by"]):
        nf, fx, nc, bk = r["by"][c]
        print(f"   {c:5s} fixed {fx:3d}/{nf:3d}  broken {bk:2d}/{nc:3d}")
    qids = list(sl["fail"]) + list(sl["control"])
    ref_path = a.runs_dir / f"{a.ref}--memspine" / "results.jsonl"
    ref_rows = read_rows(ref_path) if ref_path.exists() else {}
    f_arm = f1_summary(arm, qids, sl["category"])
    f_ref = f1_summary(ref_rows, qids, sl["category"]) if ref_rows else {}
    if f_arm:
        print("   F1 (official LoCoMo, x100; judge accuracy above is unchanged):")
        for k, (n, v) in f_arm.items():
            extra = f"  ref {f_ref[k][1]:5.1f} ({v - f_ref[k][1]:+.1f})" if k in f_ref else ""
            print(f"      {k:5s} n={n:3d} {v:5.1f}{extra}")
    if r["missing"]:
        print(f"   WARNING: {r['missing']} slice questions have no row in the arm run")
    cat = sl["category"]
    pr = paired({q: correct(d) for q, d in arm.items()}, {q: correct(d) for q, d in ref_rows.items()}, qids,
                category_of=lambda q: cat.get(q, "?"))
    print("   paired gate (G-E1):")
    for line in format_result("LoCoMo slice", pr):
        print("   " + line)
    print(f"verdict: {pr.verdict}")


if __name__ == "__main__":
    main()
