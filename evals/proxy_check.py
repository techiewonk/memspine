"""M2 proxy check: does a change in retrieval coverage track a change in LoCoMo QA?

    python proxy_check.py [--out-json proxy_check.json] [--flips-csv JUDGE_AUDIT_SHEET.csv]

Reads archived LoCoMo runs only (``runs/aamas27-locomo-qwen3--*``) and the gold evidence
in ``data/locomo10.json``. No model calls, no network.

**Per question.** ``ev_all`` = every gold evidence turn of the question (LoCoMo
``qa[*].evidence``, normalised to ``D<s>:<t>``) is among the row's ``retrieved_ids`` (the
records placed in the reader's context); ``ev_any`` = at least one is. QA = the judge score
when the row is ``completed``, else 0 (as ``score_split.py``). Categories 1-4 only.

**Per arm.** Coverage and QA are pooled over the arm's questions. Each arm is compared with
the mean of the combo-A runs of **its own era** on the same questions:

* pre-fix era (runs before the determinism fix, ADR-051): combo-A r0, r1, r2;
* post-fix era (wave of 2026-10-05..07): ``w1-combo-A`` and ``s-combo-A-r2`` (the two-run
  mean used in ``LEARNINGS_2026-10-07.md``).

**Analysis sets (fixed before the numbers were computed).**

* ``A_full`` (**primary**, decides B4): every memspine arm with all 10 conversations and
  1,540 cat 1-4 rows (resumes merged by ``query_id``), era-matched baseline.
* ``B_wave``: the 17 post-fix wave arms of ``LEARNINGS_2026-10-07.md`` §2 (B1 + the 16
  others), including subset arms, each on its own questions against the two-run mean.
* ``C_postfix_full``: the post-fix arms of ``A_full`` only.
* ``D_with_systems``: ``A_full`` plus the two full non-memspine naive-RAG runs.
* ``E_own_control_post_hoc`` (added after A-D were computed; reported, never decisive):
  each pre-fix arm against the control it was designed against (most are single changes
  on ``memspine-base``, not on combo-A), post-fix arms as in ``A_full``.

**Decision rule (B4).** The proxy is accepted for LoCoMo only if, on ``A_full``, Spearman
rho(delta ev_all, delta QA) >= 0.5 **and** the 95% percentile bootstrap CI (arms resampled
with replacement, 10,000 resamples, fixed seed) excludes 0, **and** ``B_wave`` does not
contradict it (rho > 0). A second CI resamples arms *and* conversations jointly, so that
question-sampling noise in each delta is carried into the interval.

The same run files also give the noise floor used by ``PREREG_2026-10.md`` (two-run
flip rate, per-question run variance, MDEs) and the per-conversation baseline used by the
screen protocol; ``--flips-csv`` writes the 113 judge-audit flip questions.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
GOLD = HERE / "data" / "locomo10.json"
PREFIX = "aamas27-locomo-qwen3"
CATS = ("cat1", "cat2", "cat3", "cat4")
CAT_NAMES = {"cat1": "multi-hop", "cat2": "temporal", "cat3": "open-domain", "cat4": "single-hop"}
CONVS = (
    "conv-26",
    "conv-30",
    "conv-41",
    "conv-42",
    "conv-43",
    "conv-44",
    "conv-47",
    "conv-48",
    "conv-49",
    "conv-50",
)
SEED = 20261007
RESAMPLES = 10_000
Z_A, Z_B = 1.959964, 0.841621  # two-sided alpha 0.05, power 0.80

#: Arms: name -> list of run-directory globs (relative to RUNS, without the prefix).
PRE_FIX_BASE = ("combo-A--memspine", "combo-A--r1--memspine", "combo-A--r2--memspine")
POST_FIX_BASE = ("w1-combo-A--conv-*--memspine", "s-combo-A-r2--conv-*--memspine")
PRE_FIX_ARMS: dict[str, tuple[str, ...]] = {
    **{
        a: (f"{a}--memspine",)
        for a in (
            "H1",
            "H2",
            "H2+H6",
            "H3",
            "H4",
            "H5",
            "H8",
            "H11",
            "H12-dated",
            "H14",
            "H15",
            "H16",
            "H17",
            "H21",
            "P4",
            "R-bge",
            "R-cohere",
            "combo-B",
            "memspine-base",
            "memspine-assemble",
        )
    },
    "firewall-on": ("firewall-on--memspine", "firewall-on-resume--memspine"),
    "combo-A-pool": ("combo-A-pool--memspine", "combo-A-pool-resume--memspine"),
}
POST_FIX_ARMS: dict[str, tuple[str, ...]] = {
    "B1 temporal_leg": ("s-B1-temporal-leg--conv-*--memspine",),
    "w1-cards": ("w1-cards--conv-*--memspine",),
    "w1-profile": ("w1-profile--conv-*--memspine",),
    "w1-read": ("w1-read--conv-*--memspine",),
    "w1-infer": ("w1-infer--conv-*--memspine",),
    "w1-plm": ("w1-plm--conv-*--memspine",),
    "w1-dated3": ("w1-dated3--conv-*--memspine",),
    "w2-mine2": ("w2-mine2--conv-*--memspine",),
    "w2-graph": ("w2-graph--conv-*--memspine",),
    "C1 routed prompt": ("s-C1-routed--conv-*--memspine",),
    "A1+A2+A3": ("s-A123--conv-*--memspine",),
    "A123-gated": ("s-A123-gated--conv-*--memspine",),
    "B3": ("s-B3--conv-*--memspine",),
    "D1": ("s-D1--conv-*--memspine",),
    "routed v1": ("s-routed-v1--conv-*--memspine",),
    "routed v2": ("s-routed-v2--conv-*--memspine",),
    "routed v3": ("s-routed-v3--conv-*--memspine",),
}
SYSTEMS: dict[str, tuple[str, ...]] = {
    "naive RAG dense": ("dense-naive-v2--naive-rag-dense-memspine-embedder",),
    "naive RAG BM25 1.5K": ("matched-budget--naive-rag-bm25-matched1500",),
}
#: The six wave arms behind the "solved by some arm" oracle (HEADROOM_BY_CATEGORY).
ORACLE_ARMS = ("w1-cards", "w1-profile", "w1-read", "w1-infer", "w1-plm", "w1-dated3")

_TURN = re.compile(r"D:?(\d+):(\d+)")


@dataclass
class Q:
    item: str
    cat: str
    question: str
    gold: str
    evidence: frozenset[str]


@dataclass
class Run:
    """One replicate: query_id -> (qa score, ev_all, ev_any, row)."""

    qa: dict[str, float] = field(default_factory=dict)
    ev_all: dict[str, float] = field(default_factory=dict)
    ev_any: dict[str, float] = field(default_factory=dict)
    rows: dict[str, dict] = field(default_factory=dict)


def load_gold(path: Path = GOLD) -> dict[str, Q]:
    data = json.loads(path.read_text(encoding="utf-8"))
    out: dict[str, Q] = {}
    for ci, conv in enumerate(data):
        for qi, qa in enumerate(conv.get("qa") or []):
            cat = f"cat{qa.get('category')}"
            if cat not in CATS:
                continue
            ev: set[str] = set()
            for e in qa.get("evidence") or []:
                ev.update(f"D{s}:{t}" for s, t in _TURN.findall(str(e)))
            out[f"{ci}-{qi}"] = Q(
                conv["sample_id"], cat, qa["question"], str(qa.get("answer")), frozenset(ev)
            )
    return out


def _dirs(globs: Iterable[str], runs: Path) -> list[Path]:
    found: list[Path] = []
    for g in globs:
        found.extend(sorted(runs.glob(f"{PREFIX}--{g}")))
    # originals before resumes, so a resume row replaces the original
    return sorted(found, key=lambda p: ("-resume" in p.name, p.name))


def load_run(globs: Iterable[str], gold: dict[str, Q], runs: Path = RUNS) -> Run:
    run = Run()
    for d in _dirs(globs, runs):
        path = d / "results.jsonl"
        if not path.exists():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            qid = str(row.get("query_id"))
            if row.get("kind") != "result" or qid not in gold:
                continue
            ok = row.get("status") == "completed"
            run.qa[qid] = float(row.get("score") or 0.0) if ok else 0.0
            ret = set(row.get("retrieved_ids") or ())
            ev = gold[qid].evidence
            if ev:
                run.ev_all[qid] = float(ev <= ret)
                run.ev_any[qid] = float(bool(ev & ret))
            run.rows[qid] = row
    return run


def _mean_runs(runs: Sequence[Run]) -> Run:
    out = Run()
    for attr in ("qa", "ev_all", "ev_any"):
        keys = set.intersection(*(set(getattr(r, attr)) for r in runs))
        setattr(out, attr, {k: float(np.mean([getattr(r, attr)[k] for r in runs])) for k in keys})
    return out


def _pool(values: dict[str, float], qids: Iterable[str]) -> float:
    vals = [values[q] for q in qids if q in values]
    return 100.0 * float(np.mean(vals)) if vals else math.nan


def rankdata(x: Sequence[float]) -> np.ndarray:
    a = np.asarray(x, dtype=float)
    order = a.argsort(kind="mergesort")
    ranks = np.empty(len(a))
    ranks[order] = np.arange(1, len(a) + 1)
    for v in np.unique(a):  # average ties
        m = a == v
        if m.sum() > 1:
            ranks[m] = ranks[m].mean()
    return ranks


def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    if len(x) < 3:
        return math.nan
    rx, ry = rankdata(x), rankdata(y)
    if rx.std() == 0 or ry.std() == 0:
        return math.nan
    return float(np.corrcoef(rx, ry)[0, 1])


@dataclass
class ArmDelta:
    arm: str
    era: str
    n: int
    convs: int
    qa: float
    d_qa: float
    ev_all: float
    d_ev_all: float
    d_ev_any: float
    by_cat: dict[str, tuple[float, float]]  # cat -> (d_qa, d_ev_all)
    by_conv: dict[str, tuple[float, float, int]]  # conv -> (sum d_qa, sum d_ev_all, n)


def arm_delta(name: str, era: str, run: Run, base: Run, gold: dict[str, Q]) -> ArmDelta:
    qids = sorted(set(run.qa) & set(base.qa))
    ev_q = [q for q in qids if q in run.ev_all and q in base.ev_all]
    by_cat = {}
    for c in CATS:
        cq = [q for q in qids if gold[q].cat == c]
        ce = [q for q in ev_q if gold[q].cat == c]
        by_cat[c] = (
            _pool(run.qa, cq) - _pool(base.qa, cq),
            _pool(run.ev_all, ce) - _pool(base.ev_all, ce),
        )
    by_conv: dict[str, tuple[float, float, int]] = {}
    for conv in CONVS:
        cq = [q for q in qids if gold[q].item == conv]
        if not cq:
            continue
        dq = sum(run.qa[q] - base.qa[q] for q in cq)
        de = sum(run.ev_all.get(q, 0) - base.ev_all.get(q, 0) for q in cq if q in base.ev_all)
        by_conv[conv] = (dq, de, len(cq))
    return ArmDelta(
        arm=name,
        era=era,
        n=len(qids),
        convs=len(by_conv),
        qa=_pool(run.qa, qids),
        d_qa=_pool(run.qa, qids) - _pool(base.qa, qids),
        ev_all=_pool(run.ev_all, ev_q),
        d_ev_all=_pool(run.ev_all, ev_q) - _pool(base.ev_all, ev_q),
        d_ev_any=_pool(run.ev_any, ev_q) - _pool(base.ev_any, ev_q),
        by_cat=by_cat,
        by_conv=by_conv,
    )


def rho_ci(
    arms: Sequence[ArmDelta], *, joint: bool = False, seed: int = SEED
) -> tuple[float, float]:
    """Percentile 95% CI of Spearman rho; resample arms (and conversations if ``joint``)."""
    rng = np.random.default_rng(seed)
    k = len(arms)
    convs = list(CONVS)
    # per arm per conversation sums, for the joint resample
    dq = np.array([[a.by_conv.get(c, (0, 0, 0))[0] for c in convs] for a in arms], dtype=float)
    de = np.array([[a.by_conv.get(c, (0, 0, 0))[1] for c in convs] for a in arms], dtype=float)
    nn = np.array([[a.by_conv.get(c, (0, 0, 0))[2] for c in convs] for a in arms], dtype=float)
    base_x = np.array([a.d_ev_all for a in arms])
    base_y = np.array([a.d_qa for a in arms])
    rhos = []
    for _ in range(RESAMPLES):
        idx = rng.integers(0, k, size=k)
        if joint:
            cidx = rng.integers(0, len(convs), size=len(convs))
            n = nn[:, cidx].sum(axis=1)
            with np.errstate(invalid="ignore", divide="ignore"):
                x = (de[:, cidx].sum(axis=1) / n)[idx]
                y = (dq[:, cidx].sum(axis=1) / n)[idx]
        else:
            x, y = base_x[idx], base_y[idx]
        r = spearman(x, y)
        if not math.isnan(r):
            rhos.append(r)
    lo, hi = np.quantile(rhos, [0.025, 0.975])
    return float(lo), float(hi)


def rho_perm_p(arms: Sequence[ArmDelta], seed: int = SEED) -> float:
    rng = np.random.default_rng(seed)
    x = [a.d_ev_all for a in arms]
    y = np.array([a.d_qa for a in arms])
    obs = abs(spearman(x, y))
    hits = sum(abs(spearman(x, rng.permutation(y))) >= obs - 1e-12 for _ in range(RESAMPLES))
    return (1 + hits) / (1 + RESAMPLES)


# -- noise floor and MDE --------------------------------------------------------------


def noise_floor(r1: Run, r2: Run, gold: dict[str, Q], arms: Sequence[tuple[Run, Run]]) -> dict:
    """Two-run flip rate, per-question run variance and MDEs (QA and coverage)."""
    qids = sorted(set(r1.qa) & set(r2.qa))
    out: dict = {"n": len(qids)}
    mult = Z_A + Z_B

    def block(sel: list[str], n_target: int | None = None) -> dict:
        d = np.array([r1.qa[q] - r2.qa[q] for q in sel])
        flips = float(np.mean(d != 0))
        sigma2 = float(np.mean(d**2) / 2)  # per-question variance of one run
        n = n_target or len(sel)
        se_1v1 = math.sqrt(2 * sigma2 / n)
        se_1v2 = math.sqrt(1.5 * sigma2 / n)
        # typical-arm discordance: variance of (arm - mean of the two base runs)
        arm_var = []
        for arm, base in arms:
            aq = [q for q in sel if q in arm.qa and q in base.qa]
            if len(aq) == len(sel):
                arm_var.append(float(np.var([arm.qa[q] - base.qa[q] for q in aq])))
        v_arm = float(np.median(arm_var)) if arm_var else math.nan
        cov_var = []
        for arm, base in arms:
            aq = [q for q in sel if q in arm.ev_all and q in base.ev_all]
            if aq:
                cov_var.append(float(np.var([arm.ev_all[q] - base.ev_all[q] for q in aq])))
        v_cov = float(np.median(cov_var)) if cov_var else math.nan
        return {
            "n": n,
            "flip_rate": 100 * flips,
            "null_band_1v2": 100 * Z_A * se_1v2,
            "mde_qa_null_1v1": 100 * mult * se_1v1,
            "mde_qa_null_1v2": 100 * mult * se_1v2,
            "mde_qa_typical_arm": 100 * mult * math.sqrt(v_arm / n) if v_arm == v_arm else math.nan,
            "mde_cov_typical_arm": 100 * mult * math.sqrt(v_cov / n)
            if v_cov == v_cov
            else math.nan,
        }

    out["all"] = block(qids)
    for c in CATS:
        out[c] = block([q for q in qids if gold[q].cat == c])
    out["screen_350"] = block(qids, n_target=350)
    # cluster level: per-conversation two-run deltas (percentage points)
    conv_d = []
    for conv in CONVS:
        cq = [q for q in qids if gold[q].item == conv]
        conv_d.append(100 * float(np.mean([r1.qa[q] - r2.qa[q] for q in cq])))
    sd_conv_run = float(np.std(conv_d, ddof=1)) / math.sqrt(
        2
    )  # sd of one run's conv accuracy noise
    t975, t80 = 2.262157, 0.883408  # t, df 9
    out["cluster"] = {
        "conv_two_run_deltas": dict(zip(CONVS, conv_d, strict=True)),
        "sd_conv_one_run": sd_conv_run,
        "mde_qa_cluster_1v2_10conv": (t975 + t80) * math.sqrt(1.5) * sd_conv_run / math.sqrt(10),
    }
    # retrieval determinism check: identical coverage across the two post-fix base runs
    ev_q = [q for q in qids if q in r1.ev_all and q in r2.ev_all]
    out["coverage_two_run_disagreements"] = int(sum(r1.ev_all[q] != r2.ev_all[q] for q in ev_q))
    out["coverage_n"] = len(ev_q)
    return out


# -- judge-audit flip sheet -----------------------------------------------------------


def flip_sheet(
    base1: Run, base2: Run, wave: dict[str, Run], gold: dict[str, Q], path: Path
) -> dict:
    """Temporal and single-hop questions w1-combo-A got wrong and some wave arm got right."""
    rows = []
    for qid, q in sorted(gold.items(), key=lambda kv: (kv[1].item, kv[0])):
        if q.cat not in ("cat2", "cat4") or base1.qa.get(qid, 1.0) > 0.5:
            continue
        solved = [a for a in ORACLE_ARMS if wave[a].qa.get(qid, 0.0) > 0.5]
        if not solved:
            continue
        r1 = base1.rows.get(qid, {})
        rows.append(
            {
                "audit_id": f"F{len(rows) + 1:03d}",
                "item_id": q.item,
                "query_id": qid,
                "category": f"{q.cat} {CAT_NAMES[q.cat]}",
                "question": q.question,
                "gold": q.gold,
                "comboA_r1_answer": r1.get("answer", ""),
                "comboA_r1_verdict": "correct" if base1.qa.get(qid, 0) > 0.5 else "wrong",
                "comboA_r2_verdict": ("correct" if base2.qa.get(qid, 0) > 0.5 else "wrong")
                if qid in base2.qa
                else "",
                "solved_by_arms": ";".join(solved),
                "solving_arm_answer": wave[solved[0]].rows.get(qid, {}).get("answer", ""),
                "comboA_ev_all": int(base1.ev_all.get(qid, 0)),
                "human_gold_ok": "",
                "human_comboA_verdict": "",
                "human_arm_verdict": "",
                "note": "",
            }
        )
    with path.open("w", encoding="utf-8", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r["category"]] += 1
    return {"n": len(rows), **counts}


# -- main -----------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out-json", type=Path)
    ap.add_argument("--flips-csv", type=Path)
    args = ap.parse_args(argv)

    gold = load_gold()
    pre_runs = [load_run((g,), gold) for g in PRE_FIX_BASE]
    post_runs = [load_run((g,), gold) for g in POST_FIX_BASE]
    pre_base, post_base = _mean_runs(pre_runs), _mean_runs(post_runs)

    full: list[ArmDelta] = []
    wave: list[ArmDelta] = []
    systems: list[ArmDelta] = []
    wave_runs: dict[str, Run] = {}
    pairs_for_noise: list[tuple[Run, Run]] = []
    pre_arm_runs: dict[str, Run] = {}
    for name, globs in PRE_FIX_ARMS.items():
        run = load_run(globs, gold)
        pre_arm_runs[name] = run
        d = arm_delta(name, "pre-fix", run, pre_base, gold)
        if d.n == 1540 and d.convs == 10:
            full.append(d)
    for name, globs in POST_FIX_ARMS.items():
        run = load_run(globs, gold)
        wave_runs[name] = run
        d = arm_delta(name, "post-fix", run, post_base, gold)
        wave.append(d)
        if d.n == 1540 and d.convs == 10:
            full.append(d)
            pairs_for_noise.append((run, post_base))
    for name, globs in SYSTEMS.items():
        run = load_run(globs, gold)
        systems.append(arm_delta(name, "system", run, pre_base, gold))

    def summarise(arms: Sequence[ArmDelta]) -> dict:
        x, y = [a.d_ev_all for a in arms], [a.d_qa for a in arms]
        res = {
            "k": len(arms),
            "rho_ev_all": spearman(x, y),
            "rho_ev_any": spearman([a.d_ev_any for a in arms], y),
            "pearson_ev_all": float(np.corrcoef(x, y)[0, 1]) if len(arms) > 2 else math.nan,
        }
        if len(arms) >= 4:
            res["ci_arms"] = rho_ci(arms)
            res["ci_joint"] = rho_ci(arms, joint=True)
            res["perm_p"] = rho_perm_p(arms)
        return res

    sets = {
        "A_full": summarise(full),
        "B_wave": summarise(wave),
        "C_postfix_full": summarise([a for a in full if a.era == "post-fix"]),
        "D_with_systems": summarise(full + systems),
        "A_full_pre_fix_only": summarise([a for a in full if a.era == "pre-fix"]),
    }
    # category-level: rho across arms within each category (A_full)
    for c in CATS:
        sets[f"A_full_{c}"] = {
            "k": len(full),
            "rho_ev_all": spearman([a.by_cat[c][1] for a in full], [a.by_cat[c][0] for a in full]),
        }
    # E (post hoc, reported only; does not decide B4): each pre-fix arm against the
    # control it was designed against (LOCOMO_RESULTS_2026-10-04: single changes on
    # memspine-base; H2 on memspine-assemble; combinations and rerankers on combo-A).
    own: list[ArmDelta] = []
    for d in full:
        if d.era == "post-fix":
            own.append(d)
            continue
        if d.arm in ("combo-B", "combo-A-pool", "R-bge", "R-cohere", "memspine-base"):
            ctrl = pre_base
        elif d.arm == "H2":
            ctrl = pre_arm_runs["memspine-assemble"]
        elif d.arm == "memspine-assemble":
            ctrl = pre_arm_runs["memspine-base"]
        else:
            ctrl = pre_arm_runs["memspine-base"]
        own.append(arm_delta(d.arm, "pre-fix/own", pre_arm_runs[d.arm], ctrl, gold))
    sets["E_own_control_post_hoc"] = summarise(own)
    a = sets["A_full"]
    accept = a["rho_ev_all"] >= 0.5 and a["ci_arms"][0] > 0 and sets["B_wave"]["rho_ev_all"] > 0

    w1 = load_run(("w1-combo-A--conv-*--memspine",), gold)
    r2 = load_run(("s-combo-A-r2--conv-*--memspine",), gold)
    noise = noise_floor(w1, r2, gold, pairs_for_noise)
    pre_noise = noise_floor(pre_runs[1], pre_runs[2], gold, [])

    per_conv_base = {}
    for conv in CONVS:
        cq = [q for q in post_base.qa if gold[q].item == conv]
        ce = [q for q in cq if q in post_base.ev_all]
        per_conv_base[conv] = {
            "n": len(cq),
            "qa_w1": _pool(w1.qa, cq),
            "qa_r2": _pool(r2.qa, cq),
            "qa_mean": _pool(post_base.qa, cq),
            "ev_all": _pool(post_base.ev_all, ce),
        }

    flips = None
    if args.flips_csv:
        flips = flip_sheet(w1, r2, wave_runs, gold, args.flips_csv)

    def arm_dict(d: ArmDelta) -> dict:
        return {
            "arm": d.arm,
            "era": d.era,
            "n": d.n,
            "convs": d.convs,
            "qa": d.qa,
            "d_qa": d.d_qa,
            "ev_all": d.ev_all,
            "d_ev_all": d.d_ev_all,
            "d_ev_any": d.d_ev_any,
            "by_cat": {c: {"d_qa": v[0], "d_ev_all": v[1]} for c, v in d.by_cat.items()},
            "by_conv": {
                c: {"d_qa_pts": 100 * v[0] / v[2], "d_ev_all_pts": 100 * v[1] / v[2], "n": v[2]}
                for c, v in d.by_conv.items()
            },
        }

    report = {
        "baselines": {
            "pre_fix": {
                "qa": _pool(pre_base.qa, pre_base.qa),
                "ev_all": _pool(pre_base.ev_all, pre_base.ev_all),
            },
            "post_fix": {
                "qa": _pool(post_base.qa, post_base.qa),
                "ev_all": _pool(post_base.ev_all, post_base.ev_all),
                "qa_w1": _pool(w1.qa, w1.qa),
                "qa_r2": _pool(r2.qa, r2.qa),
            },
        },
        "sets": sets,
        "decision_B4": "ACCEPT" if accept else "REJECT",
        "arms_full": [arm_dict(d) for d in full],
        "arms_wave": [arm_dict(d) for d in wave],
        "systems": [arm_dict(d) for d in systems],
        "arms_own_control": [arm_dict(d) for d in own],
        "noise_post_fix": noise,
        "noise_pre_fix_r1_r2": pre_noise,
        "per_conv_baseline": per_conv_base,
        "flips": flips,
    }
    text = json.dumps(report, indent=1, default=float)
    if args.out_json:
        args.out_json.write_text(text, encoding="utf-8")
    print(json.dumps({"sets": sets, "decision_B4": report["decision_B4"]}, indent=1, default=float))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
