"""V03: score one arm against a reference run on the BLIND validation slices (aggregate only).

    python eval_blind.py <arm-id> --ref <ref-arm-id> [--runs-dir runs] [--mab-data PATH]

Reads ``<runs>/bv-<arm>-mab--memspine/results.jsonl`` and ``bv-<arm>-cm--memspine/results.jsonl``
(the run ids written by ``run_blind_validation.sh``) and the same for the reference.

BLIND - validation only, never inspect per-question failures for design. This script therefore
prints aggregates and nothing else: per dataset the accuracy of both runs on the questions they
share, the paired net (gained minus lost, as counts), the noise band, and for MAB-CR the
supersession-order rate (``mab_gold``; needs ``--mab-data`` to recover the stale-fact map).
No question id, question, answer or per-category breakdown is ever printed or returned.

Rule (G-E1, ``memspine_evals.paired_gate``): EVERY blind dataset must be present for both runs and cover the same
questions, otherwise the verdict is INCOMPLETE (it used to confirm on the sets that happened to exist). A dataset
FAILS the guard when its paired net is below -band, band = 0.328 * sqrt(n). The arm CONFIRMS when every dataset is
complete, none fails, and the mean of the accuracy deltas is positive; NO CHANGE when nothing flips; else NEUTRAL.
Uncertainty per dataset: exact sign test and a bootstrap that resamples whole conversations (aggregates only).
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from memspine_evals.paired_gate import paired

HERE = Path(__file__).resolve().parent
DATASETS = (("mab", "MAB-CR"), ("cm", "ConvoMem"))


def run_dir(runs: Path, arm: str, key: str) -> Path:
    return runs / f"bv-{arm}-{key}--memspine"


def load_rows(path: Path) -> dict[str, dict]:
    """query_id -> the raw result row (kept in memory only; never printed)."""
    rows: dict[str, dict] = {}
    for line in path.open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") == "result":
            rows[f"{d.get('item_id', '')}/{d['query_id']}"] = d
    return rows


def is_correct(row: dict) -> bool:
    ok = row.get("status") == "completed" and bool((row.get("answer") or "").strip())
    return ok and float(row.get("score") or 0) >= 1


def compare(new: dict[str, dict], ref: dict[str, dict]) -> dict:
    """Paired aggregate of two runs over the union of their questions. Counts and rates only."""
    expected = sorted(set(new) | set(ref))
    pr = paired({q: is_correct(r) for q, r in new.items()}, {q: is_correct(r) for q, r in ref.items()}, expected)
    shared = sorted(set(new) & set(ref))
    n = len(shared)
    if not n:
        return {"n": 0, "complete": False, "missing": len(expected)}
    a_new = sum(is_correct(new[q]) for q in shared)
    a_ref = sum(is_correct(ref[q]) for q in shared)
    gained = sum(is_correct(new[q]) and not is_correct(ref[q]) for q in shared)
    lost = sum(is_correct(ref[q]) and not is_correct(new[q]) for q in shared)
    band = 0.328 * math.sqrt(n)
    return {
        "n": n,
        "acc_new": 100 * a_new / n,
        "acc_ref": 100 * a_ref / n,
        "delta": 100 * (a_new - a_ref) / n,
        "gained": gained,
        "lost": lost,
        "net": gained - lost,
        "band": band,
        "guard_ok": (gained - lost) >= -band,
        "complete": pr.verdict != "INCOMPLETE",
        "missing": pr.missing_arm + pr.missing_ref,
        "p_sign": pr.p_sign,
        "ci": (pr.ci_low, pr.ci_high),
    }


def supersession(rows: dict[str, dict], mab_data: Path) -> dict:
    """Pooled supersession-order rate of one MAB run (aggregate; needs the parquet)."""
    from memspine_evals.datasets import MemoryAgentBenchDataset
    from memspine_evals.datasets.mab_gold import supersession_order_rate

    meta = {q.query_id: q.meta for it in MemoryAgentBenchDataset(mab_data, "auto").items()
            for q in it.queries}
    return supersession_order_rate(rows.values(), meta)


def verdict(results: list[dict], expected_sets: int = len(DATASETS)) -> str:
    done = [r for r in results if r.get("n")]
    if not done:
        return "NO DATA"
    if any(not r["guard_ok"] for r in done):
        return "REJECT (guard)"
    if len(done) < expected_sets or not all(r.get("complete", True) for r in done):
        return "INCOMPLETE"
    if all(r["gained"] == 0 and r["lost"] == 0 for r in done):
        return "NO CHANGE"
    return "CONFIRMS" if sum(r["delta"] for r in done) / len(done) > 0 else "NEUTRAL"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arm")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--runs-dir", default=str(HERE / "runs"))
    ap.add_argument("--mab-data", default=None, help="Conflict_Resolution.parquet (adds supersession order)")
    a = ap.parse_args(argv)
    runs = Path(a.runs_dir)
    print("BLIND slices - aggregates only; never inspect per-question failures for design.")
    results = []
    for key, name in DATASETS:
        paths = [run_dir(runs, x, key) / "results.jsonl" for x in (a.arm, a.ref)]
        if not all(p.exists() for p in paths):
            print(f"{name}: run missing ({', '.join(p.parent.name for p in paths if not p.exists())})")
            results.append({"n": 0, "complete": False})
            continue
        new, ref = load_rows(paths[0]), load_rows(paths[1])
        r = compare(new, ref)
        results.append(r)
        if not r["n"]:
            print(f"{name}: no shared questions")
            continue
        print(f"{name} {a.arm} vs {a.ref} on {r['n']} q: {r['acc_new']:.1f} vs {r['acc_ref']:.1f} "
              f"({r['delta']:+.1f} pts)  +{r['gained']}/-{r['lost']} net {r['net']:+d} "
              f"(band +-{r['band']:.1f}) guard {'ok' if r['guard_ok'] else 'FAILED'}  sign p={r['p_sign']:.3f} "
              f"cluster 95% [{r['ci'][0]:+.0f}, {r['ci'][1]:+.0f}]"
              + ("" if r["complete"] else f"  INCOMPLETE ({r['missing']} questions missing in one run)"))
        if key == "mab" and a.mab_data:
            s_new, s_ref = supersession(new, Path(a.mab_data)), supersession(ref, Path(a.mab_data))
            print(f"   supersession order {s_new['supersession_order']} (n={s_new['n_applicable']}) "
                  f"vs {s_ref['supersession_order']} (n={s_ref['n_applicable']})")
    print(f"verdict: {verdict(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
