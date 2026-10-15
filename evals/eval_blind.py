"""V03: score one arm against a reference run on the BLIND validation slices (aggregate only).

    python eval_blind.py <arm-id> --ref <ref-arm-id> [--runs-dir runs] [--mab-data PATH] [--reexpose "<reason>"]

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

Manifests (P0-03): each run's manifest is read from the first line of its ``results.jsonl`` and passed to the gate.
When the comparability keys differ the verdict is INCOMPARABLE and only the differing manifest fields are printed
(no aggregates). A run without a manifest still scores, with a "comparability not verified" warning.

Sealed one-shot (ER-36, ``memspine_evals.seal``): the sets are scored only when every run and every question is
present and comparable. An arm (commit + flags hash) may then be scored on a set ONCE. Each scoring is appended to
``<runs>/_logs/blind_exposure.jsonl``; a second scoring of the same arm is refused (exit 2, nothing printed) unless
``--reexpose "<reason>"`` is given, and the reason is recorded. Incomplete or incomparable attempts show no
aggregate, so they expose nothing and are not recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
from pathlib import Path

from memspine_evals import seal
from memspine_evals.paired_gate import paired

HERE = Path(__file__).resolve().parent
DATASETS = (("mab", "MAB-CR"), ("cm", "ConvoMem"))
SLICE_FILES = {"mab": "blind_split_mab.json", "cm": "blind_split_convomem.json"}


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


def slice_hash(key: str, analysis: Path | None = None) -> str:
    """Content hash of the frozen blind slice for dataset ``key`` (``unknown`` if the file is absent)."""
    path = (analysis or HERE / "analysis") / SLICE_FILES[key]
    if not path.exists():
        return "unknown"
    d = json.loads(path.read_text(encoding="utf-8"))
    if d.get("split_sha256"):
        return str(d["split_sha256"])
    return hashlib.sha256("\n".join(sorted(d.get("blind_items", []))).encode()).hexdigest()[:16]


def compare(new: dict[str, dict], ref: dict[str, dict], arm_manifest: dict | None = None,
            ref_manifest: dict | None = None) -> dict:
    """Paired aggregate of two runs over the union of their questions. Counts and rates only."""
    expected = sorted(set(new) | set(ref))
    pr = paired({q: is_correct(r) for q, r in new.items()}, {q: is_correct(r) for q, r in ref.items()}, expected,
                arm_manifest=arm_manifest, ref_manifest=ref_manifest)
    if pr.verdict == "INCOMPARABLE":
        return {"n": len(set(new) & set(ref)), "complete": False, "incomparable": pr.incomparable_fields,
                "missing": pr.missing_arm + pr.missing_ref, "warnings": pr.warnings}
    shared = sorted(set(new) & set(ref))
    n = len(shared)
    if not n:
        return {"n": 0, "complete": False, "missing": len(expected), "warnings": pr.warnings}
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
        "complete": not (pr.missing_arm or pr.missing_ref),
        "missing": pr.missing_arm + pr.missing_ref,
        "p_sign": pr.p_sign,
        "ci": (pr.ci_low, pr.ci_high),
        "warnings": pr.warnings,
    }


def supersession(rows: dict[str, dict], mab_data: Path) -> dict:
    """Pooled supersession-order rate of one MAB run (aggregate; needs the parquet)."""
    from memspine_evals.datasets import MemoryAgentBenchDataset
    from memspine_evals.datasets.mab_gold import supersession_order_rate

    meta = {q.query_id: q.meta for it in MemoryAgentBenchDataset(mab_data, "auto").items()
            for q in it.queries}
    return supersession_order_rate(rows.values(), meta)


def verdict(results: list[dict], expected_sets: int = len(DATASETS)) -> str:
    if any(r.get("incomparable") for r in results):
        return "INCOMPARABLE"
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


def _load_sets(arm: str, ref: str, runs: Path) -> tuple[list[dict], list[str]]:
    """Load and compare every blind set silently (nothing printed). -> (sets, reasons the sets cannot be scored)."""
    sets: list[dict] = []
    problems: list[str] = []
    for key, name in DATASETS:
        paths = [run_dir(runs, x, key) / "results.jsonl" for x in (arm, ref)]
        if not all(p.exists() for p in paths):
            problems.append(f"{name}: run missing ({', '.join(p.parent.name for p in paths if not p.exists())})")
            continue
        new, ref_rows = load_rows(paths[0]), load_rows(paths[1])
        m_new, m_ref = seal.read_manifest(paths[0]), seal.read_manifest(paths[1])
        r = compare(new, ref_rows, m_new, m_ref)
        if r.get("incomparable"):
            problems.append(f"{name}: INCOMPARABLE, manifests differ on " + "; ".join(r["incomparable"]))
        elif not r["n"]:
            problems.append(f"{name}: no shared questions")
        elif not r["complete"]:
            problems.append(f"{name}: INCOMPLETE ({r['missing']} questions missing in one run)")
        sets.append({"key": key, "name": name, "new": new, "ref": ref_rows, "m_new": m_new, "r": r})
    return sets, problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("arm")
    ap.add_argument("--ref", required=True)
    ap.add_argument("--runs-dir", default=str(HERE / "runs"))
    ap.add_argument("--analysis-dir", default=None, help="folder of the frozen blind_split_*.json (default analysis/)")
    ap.add_argument("--ledger", default=None, help="exposure ledger (default <runs-dir>/_logs/blind_exposure.jsonl)")
    ap.add_argument("--reexpose", default=None, metavar="REASON",
                    help="score an arm a second time on a sealed set; the reason is recorded in the ledger")
    ap.add_argument("--mab-data", default=None, help="Conflict_Resolution.parquet (adds supersession order)")
    a = ap.parse_args(argv)
    runs = Path(a.runs_dir)
    ledger = Path(a.ledger) if a.ledger else seal.default_ledger(runs)
    analysis = Path(a.analysis_dir) if a.analysis_dir else None
    print("BLIND slices - aggregates only; never inspect per-question failures for design.")
    sets, problems = _load_sets(a.arm, a.ref, runs)
    if problems:
        # no aggregate is shown and nothing is recorded: an unusable attempt does not spend the one look
        for p in problems:
            print(p)
        word = "INCOMPARABLE" if any(s["r"].get("incomparable") for s in sets) else "INCOMPLETE"
        print(f"verdict: {word}  (sealed sets not scored, no exposure recorded)")
        return 0
    arm = {s["key"]: seal.arm_id(a.arm, s["m_new"]) for s in sets}
    prior: dict[str, int] = {}
    try:
        for s in sets:
            prior[s["key"]] = seal.check(ledger, f"blind-{s['key']}", arm[s["key"]], a.reexpose)
    except seal.SealRefused as exc:
        print(f"REFUSED: {exc}")
        return 2
    verdict_text = "ERROR"
    try:
        results = []
        for s in sets:
            r = s["r"]
            results.append(r)
            for w in dict.fromkeys(r.get("warnings", [])):
                print(f"{s['name']}: WARNING: {w}")
            print(f"{s['name']} {a.arm} vs {a.ref} on {r['n']} q: {r['acc_new']:.1f} vs {r['acc_ref']:.1f} "
                  f"({r['delta']:+.1f} pts)  +{r['gained']}/-{r['lost']} net {r['net']:+d} "
                  f"(band +-{r['band']:.1f}) guard {'ok' if r['guard_ok'] else 'FAILED'}  sign p={r['p_sign']:.3f} "
                  f"cluster 95% [{r['ci'][0]:+.0f}, {r['ci'][1]:+.0f}]")
            if s["key"] == "mab" and a.mab_data:
                s_new, s_ref = supersession(s["new"], Path(a.mab_data)), supersession(s["ref"], Path(a.mab_data))
                print(f"   supersession order {s_new['supersession_order']} (n={s_new['n_applicable']}) "
                      f"vs {s_ref['supersession_order']} (n={s_ref['n_applicable']})")
        verdict_text = verdict(results)
        print(f"verdict: {verdict_text}")
    finally:
        # aggregates were (or may have been) shown, so the exposure counts even if scoring then failed
        for s in sets:
            seal.record(ledger, set_id=f"blind-{s['key']}", slice_hash=slice_hash(s["key"], analysis),
                        arm=arm[s["key"]], ref_run=a.ref, manifest=s["m_new"], verdict=verdict_text,
                        reexpose=a.reexpose if prior.get(s["key"]) else None, prior=prior.get(s["key"], 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
