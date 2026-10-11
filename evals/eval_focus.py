"""Score a focus-slice screen run (FAIL questions + CONTROL sample) against the reference run.

    python eval_focus.py ARM_RUN [--ref r7-protect-full] [--slice analysis/focus_slice_r7protect.json]
    python eval_focus.py ARM_OPB_RUN --opb [--slice analysis/focus_slice_opb.json]

LoCoMo: fixed = FAIL questions now correct; broken = CONTROL questions now wrong (correct rule as eval_screen.py).
  estimated full-set net = fixed - broken * (n_correct_total / n_control)      (control is a sample of the correct set)
  band = 0.328*sqrt(n_total); PROMOTE iff est. net > band and broken-rate <= 3%; HOLD if net > 0 but not enough;
  else REJECT. The slice is the baseline's own failures, so a bare "fixed" count is never the verdict.
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
    ap.add_argument("--ref", default="r7-protect-full", help="reference run (recorded in the slice; informational)")
    ap.add_argument("--slice", type=Path, default=None)
    ap.add_argument("--opb", action="store_true", help="force OP-Bench scoring (auto if opbench_summary.json exists)")
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    a = ap.parse_args()
    run_dir = a.runs_dir / f"{a.arm_run}--memspine"
    is_opb = a.opb or (run_dir / "opbench_summary.json").exists()
    sl_path = a.slice or HERE / "analysis" / ("focus_slice_opb.json" if is_opb else "focus_slice_r7protect.json")
    sl = json.loads(sl_path.read_text(encoding="utf-8"))
    if a.ref != sl["source_run"] and not is_opb:
        print(f"note: slice was built from {sl['source_run']}, not {a.ref}")
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
    print(f"LoCoMo {a.arm_run} vs {sl['source_run']} (slice {sl_path.name} {sl['hash'][:10]})")
    print(f"   fixed  {r['fixed']}/{r['n_fail']} FAIL questions now correct")
    print(f"   broken {r['broken']}/{r['n_control']} CONTROL questions now wrong ({100*r['broken_rate']:.1f}%, max {100*BROKEN_MAX:.0f}%)")
    print(f"   est. full-set net {r['net']:+.1f} q = fixed - broken*{r['scale']:.2f}  (band +-{r['band']:.1f} = 0.328*sqrt({sl['n_total']}))")
    for c in sorted(r["by"]):
        nf, fx, nc, bk = r["by"][c]
        print(f"   {c:5s} fixed {fx:3d}/{nf:3d}  broken {bk:2d}/{nc:3d}")
    if r["missing"]:
        print(f"   WARNING: {r['missing']} slice questions have no row in the arm run")
    print(f"verdict: {r['verdict']}")


if __name__ == "__main__":
    main()
