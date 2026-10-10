"""Score one screen against the cross-benchmark baseline on BOTH in-scope benchmarks (average-led rule).

    python eval_screen.py <screen-id> [--loc-ref xb-loc-dev] [--opb-ref xb-opb-dev]

LoCoMo: paired per question against the reference on the same questions (`<id>-loc`, conv-26/30 cat 1-5).
OP-Bench: subscores and official overall of `<id>-opb` against the reference run's `opbench_summary.json`.
Adoption (GENERALISATION_AUDIT 4.3, user rule 2026-10-10): the macro mean of the per-benchmark point deltas must be
positive beyond noise, and no benchmark may lose more than its noise band (LoCoMo 304 q: +-5.7 q = 0.328*sqrt(n);
OP-Bench: +-3 points on the official overall, a provisional band until repeat runs exist, see noise_floor.py).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

RUNS = Path(__file__).parent / "runs"
CATS = (("cat4", "single"), ("cat1", "multi"), ("cat2", "temporal"), ("cat3", "open"), ("cat5", "cat5"))
SUBS = (
    "irrelevance_fully_irrelevant",
    "irrelevance_baiting",
    "sycophancy_fact",
    "sycophancy_value",
    "sycophancy_memory",
    "repetition",
)
OPB_BAND = 3.0


def locomo(run: str) -> dict[str, tuple[bool, str]]:
    out = {}
    path = RUNS / f"{run}--memspine" / "results.jsonl"
    for line in path.open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") != "result":
            continue
        sc = d.get("score_date_checked", d.get("score"))
        ok = float(sc or 0) >= 1 and d["status"] == "completed" and bool((d.get("answer") or "").strip())
        out[d["query_id"]] = (ok, d["type_label"])
    return out


def opbench(run: str) -> dict | None:
    path = RUNS / f"{run}--memspine" / "opbench_summary.json"
    if not path.exists():
        return None
    o = json.loads(path.read_text(encoding="utf-8"))["official"]
    tt, st = o.get("by_task_type", {}), o.get("sycophancy_subtypes", {})

    def avg(d, k):
        return float(d[k]["average"]) if k in d else None

    flat = {
        "irrelevance_fully_irrelevant": avg(tt, "irrelevance_easy"),
        "irrelevance_baiting": avg(tt, "irrelevance_hard"),
        "sycophancy_fact": avg(st, "sycophancy/fact"),
        "sycophancy_value": avg(st, "sycophancy/value"),
        "sycophancy_memory": avg(st, "sycophancy/memory"),
        "repetition": avg(tt, "diversity"),
        "official_overall": float(o["overall"]["average"]),
    }
    return {k: v for k, v in flat.items() if v is not None}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("screen")
    ap.add_argument("--loc-ref", default="xb-loc-dev")
    ap.add_argument("--opb-ref", default="xb-opb-dev")
    ap.add_argument("--loc-suffix", default="loc", help="LoCoMo run suffix: loc (304-q screens) or full (1,540 q)")
    a = ap.parse_args()
    deltas = []
    guard_ok = True

    loc_run = f"{a.screen}-{a.loc_suffix}"
    if (RUNS / f"{loc_run}--memspine" / "results.jsonl").exists():
        ref, new = locomo(a.loc_ref), locomo(loc_run)
        qs = [q for q in new if q in ref]
        gained = [q for q in qs if new[q][0] and not ref[q][0]]
        lost = [q for q in qs if ref[q][0] and not new[q][0]]
        acc_r = 100 * sum(ref[q][0] for q in qs) / len(qs)
        acc_n = 100 * sum(new[q][0] for q in qs) / len(qs)
        band = 0.328 * math.sqrt(len(qs))
        net = len(gained) - len(lost)
        print(f"LoCoMo {loc_run} vs {a.loc_ref} on {len(qs)} q: {acc_n:.1f} vs {acc_r:.1f}  +{len(gained)}/-{len(lost)} net {net:+d} (band +-{band:.1f})")
        for c, name in CATS:
            cq = [q for q in qs if new[q][1] == c]
            if cq:
                print(f"   {name:8s} n={len(cq):3d} {100*sum(new[q][0] for q in cq)/len(cq):5.1f} vs {100*sum(ref[q][0] for q in cq)/len(cq):5.1f}")
        deltas.append(acc_n - acc_r)
        if net < -band:
            guard_ok = False
    else:
        print(f"LoCoMo: no {loc_run} yet")

    ref_o, new_o = opbench(a.opb_ref), opbench(f"{a.screen}-opb")
    if ref_o and new_o:
        print(f"OP-Bench {a.screen}-opb vs {a.opb_ref} (points, x100):")
        for k in SUBS:
            if k in ref_o and k in new_o:
                print(f"   {k:30s} {100*new_o[k]:6.1f} vs {100*ref_o[k]:6.1f}  {100*(new_o[k]-ref_o[k]):+6.1f}")
        key = next((k for k in ("official_overall", "overall") if k in ref_o and k in new_o), None)
        if key:
            d = 100 * (new_o[key] - ref_o[key])
            print(f"   official overall               {100*new_o[key]:6.1f} vs {100*ref_o[key]:6.1f}  {d:+6.1f} (band +-{OPB_BAND})")
            deltas.append(d)
            if d < -OPB_BAND:
                guard_ok = False
    else:
        print(f"OP-Bench: no {a.screen}-opb summary yet")

    if deltas:
        macro = sum(deltas) / len(deltas)
        verdict = "ADOPT-CANDIDATE" if guard_ok and macro > 1.0 else ("REJECT (guard)" if not guard_ok else "NEUTRAL")
        print(f"macro mean delta {macro:+.2f} pts over {len(deltas)} benchmark(s); guard {'ok' if guard_ok else 'FAILED'} -> {verdict}")


if __name__ == "__main__":
    main()
