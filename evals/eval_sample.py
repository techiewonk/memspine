"""DESCRIPTIVE score of a sample-slice run against its reference run (RUNBOOK2). Never a PROMOTE verdict.

    python eval_sample.py ARM_RUN --ref REF_RUN --set loc|opb --screen S13 --arm ARM [--commit REV] [--notes TEXT]
                          [--slice analysis/sample_slice_...json] [--arm-file arms/ARM.json] [--no-ledger]

Prints wins / losses / ties, net, delta (accuracy points for LoCoMo, mean score points x100 for OP-Bench), a paired
bootstrap interval over the sampled questions (2000 draws, seed 7: a spread, not a test), per-category wins/losses,
cost (tokens, USD) and latency p50/p95, and appends one row to ``runs/_logs/v2_results.tsv`` (``v2_ledger.py``).
A sample of 60-100 questions cannot carry an adoption decision; it only ranks arms and shows where to iterate. The
paired gate (``paired_gate.py``) is used here only for the manifest comparability check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

import v2_ledger
from memspine_evals.manifest import comparability_key, percentile
from memspine_evals.paired_gate import compare_manifests
from memspine_evals.seal import read_manifest

HERE = Path(__file__).parent
BOOT_REPS = 2000
BOOT_SEED = 7
OPB_TIE = 0.05  # graded OP-Bench scores closer than this are a tie (judge noise)
SLICE_FILE = {"loc": "sample_slice_loc_80_s20261013.json", "opb": "sample_slice_opb_60_s20261013.json"}


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


def opb_scores(run_dir: Path, rows: dict[str, dict]) -> dict[str, float]:
    per = json.loads((run_dir / "opbench_summary.json").read_text(encoding="utf-8"))["per_probe"]
    return {k: float(per[d["query_id"]]) for k, d in rows.items() if d["query_id"] in per}


def describe(arm: dict[str, float], ref: dict[str, float], ids: list[str], cat: dict[str, str], tie: float = 0.0,
             scale: float = 100.0) -> dict:
    """Paired descriptive statistics over the ids present in both runs (scores are 0/1 or graded 0..1)."""
    shared = [q for q in ids if q in arm and q in ref]
    diffs = {q: arm[q] - ref[q] for q in shared}
    wins = [q for q in shared if diffs[q] > tie]
    losses = [q for q in shared if diffs[q] < -tie]
    n = len(shared)
    delta = scale * sum(diffs.values()) / n if n else 0.0
    lo = hi = delta
    if n:
        rng = random.Random(BOOT_SEED)
        vals = [diffs[q] for q in shared]
        draws = sorted(scale * sum(rng.choice(vals) for _ in range(n)) / n for _ in range(BOOT_REPS))
        lo, hi = draws[int(0.025 * BOOT_REPS)], draws[int(0.975 * BOOT_REPS) - 1]
    by: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0])
    for q in shared:
        c = cat.get(q, "?")
        by[c][0 if q in wins else 1 if q in losses else 2] += 1
    return {"n": n, "n_expected": len(ids), "wins": len(wins), "losses": len(losses), "ties": n - len(wins) - len(losses),
            "net": len(wins) - len(losses), "delta": delta, "ci_low": lo, "ci_high": hi,
            "by_category": {c: tuple(v) for c, v in sorted(by.items())}}


def cost_latency(rows: dict[str, dict], ids: list[str]) -> tuple[int, float, float | None, float | None]:
    tokens, usd, lat = 0, 0.0, []
    for q in ids:
        d = rows.get(q)
        if not d:
            continue
        tokens += int(float(d.get("prompt_tokens") or 0)) + int(float(d.get("completion_tokens") or 0))
        meta = d.get("meta") or {}
        usd += float(meta.get("spend_usd") or 0) if isinstance(meta, dict) else 0.0
        lat.append(sum(float(d.get(k) or 0) for k in ("latency_retrieve_ms", "latency_answer_ms", "latency_judge_ms")))
    return tokens, usd, percentile(lat, 50) if lat else None, percentile(lat, 95) if lat else None


def file_digest(path: Path | None) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:10] if path and path.exists() else ""


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("arm_run")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--set", dest="which", choices=sorted(SLICE_FILE), required=True)
    ap.add_argument("--slice", type=Path, default=None)
    ap.add_argument("--screen", default="")
    ap.add_argument("--arm", default="")
    ap.add_argument("--arm-file", type=Path, default=None)
    ap.add_argument("--commit", default="")
    ap.add_argument("--mode", default="sample")
    ap.add_argument("--notes", default="")
    ap.add_argument("--no-ledger", action="store_true")
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    a = ap.parse_args()
    if a.arm_run == a.ref:
        ap.error("--ref must name a different run than the arm")
    sl = json.loads((a.slice or HERE / "analysis" / SLICE_FILE[a.which]).read_text(encoding="utf-8"))
    ids, cat = list(sl["query_ids"]), sl.get("category", {})
    arm_dir, ref_dir = a.runs_dir / f"{a.arm_run}--memspine", a.runs_dir / f"{a.ref}--memspine"
    arm_rows, ref_rows = read_rows(arm_dir / "results.jsonl"), read_rows(ref_dir / "results.jsonl")
    if a.which == "opb":
        arm_s, ref_s, tie, unit = opb_scores(arm_dir, arm_rows), opb_scores(ref_dir, ref_rows), OPB_TIE, "points of mean score"
    else:
        arm_s = {q: float(correct(d)) for q, d in arm_rows.items()}
        ref_s = {q: float(correct(d)) for q, d in ref_rows.items()}
        tie, unit = 0.0, "accuracy points"
    r = describe(arm_s, ref_s, ids, cat, tie)
    am, rm = read_manifest(arm_dir / "results.jsonl"), read_manifest(ref_dir / "results.jsonl")
    diff = compare_manifests(am, rm) if am and rm else []
    key = comparability_key(am) if am else ""
    tokens, usd, p50, p95 = cost_latency(arm_rows, ids)
    per_cat = ";".join(f"{c}:+{w}/-{lo}/={t}" for c, (w, lo, t) in r["by_category"].items())
    print(f"SAMPLE {a.arm_run} vs {a.ref} ({a.which}, slice {sl['slice_id']} {sl['hash'][:10]}): DESCRIPTIVE ONLY, no verdict")
    print(f"   n={r['n']}/{r['n_expected']}  wins {r['wins']}  losses {r['losses']}  ties {r['ties']}  net {r['net']:+d}")
    print(f"   delta {r['delta']:+.2f} {unit}, paired bootstrap 95% [{r['ci_low']:+.2f}, {r['ci_high']:+.2f}] (a spread over the sample, not a test)")
    for c, (w, lo, t) in r["by_category"].items():
        print(f"   {c:20s} +{w}/-{lo}/={t}")
    print(f"   cost {tokens} tokens, ${usd:.4f}; latency p50 {p50 or 0:.0f} ms, p95 {p95 or 0:.0f} ms (retrieve+answer+judge)")
    if r["n"] < r["n_expected"]:
        print(f"   WARNING: {r['n_expected'] - r['n']} sample questions missing from the arm or the reference run")
    if diff:
        print(f"   WARNING: manifests differ on {', '.join(diff)} (comparison is not like for like)")
    elif not (am and rm):
        print("   WARNING: manifest missing: comparability not verified")
    if not a.no_ledger:
        v2_ledger.append({
            "mode": a.mode, "screen": a.screen, "arm": a.arm or a.arm_run, "arm_digest": file_digest(a.arm_file),
            "reference": a.ref, "commit": a.commit, "slice_id": sl["slice_id"], "slice_hash": sl.get("manifest_hash", sl["hash"][:16]),
            "n": r["n"], "wins": r["wins"], "losses": r["losses"], "ties": r["ties"], "net": r["net"],
            "delta": f"{r['delta']:.3f}", "ci_low": f"{r['ci_low']:.3f}", "ci_high": f"{r['ci_high']:.3f}",
            "per_category": per_cat, "cost": f"tokens={tokens};usd={usd:.4f}",
            "latency_p50_ms": f"{p50 or 0:.0f}", "latency_p95_ms": f"{p95 or 0:.0f}", "comparability_key": key,
            "verdict": "DESCRIPTIVE", "notes": (a.notes + (" | INCOMPARABLE: " + ",".join(diff) if diff else "")).strip(" |"),
        })
        print(f"   ledger: {v2_ledger.results_path()}")


if __name__ == "__main__":
    main()
