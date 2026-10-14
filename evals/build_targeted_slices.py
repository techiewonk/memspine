"""Failure-enriched ("targeted") LoCoMo slices for the GPU screens (FAIL-SLICE).

    python build_targeted_slices.py [--runs-dir DIR] [--seed 20261013] [--out-dir analysis] [--check]

Why. A random 80-question sample holds 13-15 wrong questions, almost none in the class a screen targets, so a screen
cannot show whether it fixes its class. Reference reruns are deterministic (0 of 80 flips), so every flip is caused by the
screen. A targeted slice is therefore: all wrong questions of one class family (cap ``CAP_WRONG``) plus a matched set of
CORRECT questions (controls, about 40% of the slice) drawn from the same conversations and categories with a fixed seed.

Inputs (ids and codes only): ``analysis/locomo_problem_labels_clean_jina_full.txt`` (one line per wrong question of the
reference run: ``item qid stage class``), the class names of ``analysis/locomo_solution_map.json`` and the reference run's
``results.jsonl`` (read for ids, category and the correct/wrong flag only; no text is copied). If ``<evals>/runs`` is absent
(a worktree) the shared store of the sibling ``memspine`` checkout is used.

Outputs, committed under ``analysis/`` (same keys as ``sample_slice_*.json`` plus the targeted fields below):

    fail_slice_<name>_s<seed>.json        all conversations that hold the family's questions
    fail_slice_<name>_s<seed>_c3.json     the three conversations with the most family failures (engine-LLM rows are ingest-bound)
    ../v2_slice_map.tsv                   screen -> slice mapping read by run_v2_queue.sh (see ``SCREEN_OVERRIDES``)

Families (class names are those of the solution map):

    fail_list       list_aggregation_generic, list_incomplete, count_wrong
    fail_retrieval  open_domain_inference_retrieval, temporal_evidence_missing, multi_evidence_recall,
                    vocabulary_mismatch_recall, implicit_or_paraphrased_gold
    fail_fusion     fusion_loss_*, rerank_keep_cut, lexical_outranked_recall
    fail_temporal   date_*, relative_date_gold_vs_judge
    fail_reader     wrong_detail, refusal_premise_denial, inference_open_domain, distractor_line, image_caption_misuse,
                    speaker_confusion
    fail_mix_120    60 wrong across all fixable classes + 60 controls

NEVER targets: gold_error, judge_false_negative, no_gold_resolvable, write_path_loss, stage (the engine cannot fix them).

Reading rule. A targeted slice is NOT an estimate of overall accuracy. Read two numbers: target-class questions recovered /
wrong target questions in the slice (``tgt_rec``), and controls lost / controls (``ctl_lost``), see ``eval_sample.py``.

Conversation policy. The held-out policy of ``build_forensics_explorer.py`` drops conv-43/44/47/48/49/50 by default; the
owner allowed all 10 conversations for the logged full run (2026-10-10) and this builder keeps that. Each slice records
``uses_heldout_conversations`` and the dev / held-out split of the conversations it uses, so a reader can tell.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

from make_sample_slice import digest, largest_remainder, manifest_hash

HERE = Path(__file__).parent
SEED = 20261013
SOURCE_RUN = "clean-jina-full"
CAP_WRONG = 60
CONTROL_SHARE = 0.4  # controls / slice
LABELS = "analysis/locomo_problem_labels_clean_jina_full.txt"
SPLIT = "analysis/locomo_split.json"
SOLUTION_MAP = "analysis/locomo_solution_map.json"

FAMILIES: dict[str, tuple[str, ...]] = {
    "fail_list": ("list_aggregation_generic", "list_incomplete", "count_wrong"),
    "fail_retrieval": ("open_domain_inference_retrieval", "temporal_evidence_missing", "multi_evidence_recall",
                       "vocabulary_mismatch_recall", "implicit_or_paraphrased_gold"),
    "fail_fusion": ("fusion_loss_lexical_only", "fusion_loss_vector_only", "fusion_loss_two_leg_crowded",
                    "rerank_keep_cut", "lexical_outranked_recall"),
    "fail_temporal": ("date_duration_arithmetic", "date_offset_small", "date_wrong_event", "relative_date_gold_vs_judge"),
    "fail_reader": ("wrong_detail", "refusal_premise_denial", "inference_open_domain", "distractor_line",
                    "image_caption_misuse", "speaker_confusion"),
}
NEVER_TARGET = ("gold_error", "judge_false_negative", "no_gold_resolvable", "write_path_loss", "stage", "ok")
FIXABLE = tuple(c for cls in FAMILIES.values() for c in cls)
MIX = "fail_mix_120"
MIX_WRONG = 60
CLASS_FLOOR = {"family": 3, "mix": 2}  # every class that has failures keeps at least this many in a capped slice

# Screens not listed here are mapped from the solution map: the family(ies) holding the screen's PRIMARY target classes
# (secondary classes if it has no primary), most primary classes first, at most two. Rows with rule PARITY / REF / REPORT
# and screens without target classes keep the random sample slices. Owner examples below win over the derivation.
SCREEN_OVERRIDES: dict[str, tuple[str, ...]] = {
    "S34": ("fail_list",), "S35": ("fail_list",), "S40": ("fail_list",),
    "S60": ("fail_fusion", MIX), "S67": ("fail_fusion", MIX), "S14": ("fail_fusion", MIX), "S51": ("fail_fusion", MIX),
    "S55": ("fail_retrieval",), "S56": ("fail_list", "fail_retrieval"),  # S56: list/count targets, sits on the S55 deep reader
    "S61": ("fail_retrieval",),
    "S62": ("fail_temporal",),
    "S63": ("fail_reader",),  # plus the OP-Bench sample slice, which the queue keeps for the 'o' set
    "S20": (MIX,),  # the relevance gate must not break correct category 1-4 answers; its category-5 row is CAT5_SCREENS
    # R14 builds (docs/v2/research/R14_MASTER.md): primary family plus the mix as the guard
    "S73": ("fail_reader",),  # read.inference_mode; its category-5 row is CAT5_SCREENS (must still abstain)
    "S74": ("fail_reader",),  # read.inference_mode=gated_v2 (widened shape); cat-5 row is CAT5_SCREENS
    "S77": ("fail_list", MIX), "S79": ("fail_list", MIX),
    "S82": ("fail_fusion", MIX), "S87": ("fail_temporal", MIX), "S69": ("fail_retrieval", MIX),
    "S70": ("fail_retrieval", MIX),  # R14-IVFIX: S69 with the anaphoric carry rule
}
# S20-CAT5: screens whose arm gates or abstains on irrelevant evidence also run on the LoCoMo category-5 (adversarial)
# slices of make_cat5_slice.py. The row carries a sixth column ``categories=5`` that the queue turns into ``--categories 5``
# (the reference run uses the same categories and a distinct cached name). S20 = read.gate_v2; the S55 / S63 arms contain it.
CAT5_SCREENS = ("S20", "S55", "S63", "S73", "S74")  # S73/S74: the gated inference clause must not commit on cat 5
CAT5_SLICE = "analysis/sample_slice_loc_cat5_s{seed}.json"
CAT5_SLICE_C3 = "analysis/sample_slice_loc_cat5_s{seed}_c3.json"
CAT5_COLUMN = "categories=5"
MAP_RULES = ("PROMOTE", "NONINF")


# ------------------------------------------------------------------------------------------------ inputs
def family_of(cls: str) -> str | None:
    for fam, classes in FAMILIES.items():
        if cls in classes:
            return fam
    return None


def read_labels(path: Path) -> dict[str, str]:
    """{"conv-26/0-15": class} for every wrong question of the reference run (ids and codes only)."""
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 4:
            out[f"{parts[0]}/{parts[1]}"] = parts[3]
    return out


def find_runs_dir(explicit: Path | None = None) -> Path:
    cands = [explicit] if explicit else [HERE / "runs", HERE.parents[1] / "memspine" / "evals" / "runs"]
    for c in cands:
        if c and (c / f"{SOURCE_RUN}--memspine" / "results.jsonl").exists():
            return c
    raise SystemExit(f"no {SOURCE_RUN}--memspine/results.jsonl under {[str(c) for c in cands if c]}; pass --runs-dir")


def read_universe(runs: Path) -> tuple[list[str], dict[str, tuple[str, str]], set[str]]:
    """(ids in dataset order, {id: (category, conversation)}, ids answered correctly by the reference run)."""
    order, meta, right = [], {}, set()
    for line in (runs / f"{SOURCE_RUN}--memspine" / "results.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") != "result":
            continue
        q = f"{d['item_id']}/{d['query_id']}"
        sc = d.get("score_date_checked", d.get("score"))
        order.append(q)
        meta[q] = (str(d.get("type_label") or "?"), str(d["item_id"]))
        if float(sc or 0) >= 1 and d.get("status") == "completed" and (d.get("answer") or "").strip():
            right.add(q)
    return order, meta, right


# ------------------------------------------------------------------------------------------------ selection
def pick_wrong(cands: dict[str, str], cap: int, rng: random.Random, floor: int) -> list[str]:
    """Up to ``cap`` of the wrong ids {id: class}: all of them if they fit, else a class-stratified seeded draw."""
    if len(cands) <= cap:
        return sorted(cands)
    by_cls: dict[str, list[str]] = defaultdict(list)
    for q, c in cands.items():
        by_cls[c].append(q)
    size = {c: len(v) for c, v in by_cls.items()}
    fl = {c: min(floor, size[c]) for c in size}
    if sum(fl.values()) > cap:
        fl = largest_remainder({c: 1 for c in size}, cap, caps=size)
    extra = largest_remainder({c: size[c] - fl[c] for c in size}, cap - sum(fl.values()),
                              caps={c: size[c] - fl[c] for c in size})
    out: list[str] = []
    for c in sorted(by_cls):
        out += rng.sample(sorted(by_cls[c]), fl[c] + extra[c])
    return sorted(out)


def pick_controls(wrong: list[str], meta: dict, right_ids: list[str], n_ctl: int, rng: random.Random) -> list[str]:
    """Correct questions matched to the wrong ones by (conversation, category) cell, largest-remainder, seeded draw.

    Cells with too few correct questions hand their shortfall to the same conversation (any category), then to the
    slice's other conversations, so the total is n_ctl whenever enough correct questions exist there."""
    cell_w = Counter((meta[q][1], meta[q][0]) for q in wrong)
    pool: dict[tuple[str, str], list[str]] = defaultdict(list)
    for q in right_ids:
        pool[(meta[q][1], meta[q][0])].append(q)
    cells = sorted(cell_w)
    caps = {k: len(pool.get(k, [])) for k in cells}
    alloc = largest_remainder({k: cell_w[k] for k in cells}, n_ctl, caps=caps)
    chosen: list[str] = []
    for k in cells:
        chosen += rng.sample(sorted(pool.get(k, [])), alloc[k])
    taken = set(chosen)
    convs = sorted({k[0] for k in cells})
    # shortfall: same conversations, any category, in proportion to the wrong count of the conversation
    need = n_ctl - len(chosen)
    if need > 0:
        rest = {v: [q for q in right_ids if meta[q][1] == v and q not in taken] for v in convs}
        w_conv = Counter(meta[q][1] for q in wrong)
        extra = largest_remainder({v: w_conv[v] for v in convs}, need, caps={v: len(rest[v]) for v in convs})
        for v in convs:
            picked = rng.sample(sorted(rest[v]), extra[v])
            chosen += picked
            taken.update(picked)
    return sorted(chosen)


def make_slice(name: str, wrong_by_cls: dict[str, str], order: list[str], meta: dict, right: set[str], split: dict,
               seed: int, convs: int | None, cap: int = CAP_WRONG, floor: int = CLASS_FLOOR["family"],
               share: float = CONTROL_SHARE, n_wrong_exact: int | None = None, n_ctl_exact: int | None = None) -> dict:
    """One slice. ``wrong_by_cls`` = {id: class} of the family's candidate wrong questions."""
    cands = dict(wrong_by_cls)
    if convs:
        top = Counter(meta[q][1] for q in cands)
        keep = [v for v, _ in sorted(top.items(), key=lambda kv: (-kv[1], kv[0]))[:convs]]
        cands = {q: c for q, c in cands.items() if meta[q][1] in keep}
    rng = random.Random(f"{seed}:{name}:{convs or 'all'}")
    k = n_wrong_exact if n_wrong_exact is not None else cap
    wrong = pick_wrong(cands, min(k, len(cands)), rng, floor)
    n_ctl = n_ctl_exact if n_ctl_exact is not None else int(round(len(wrong) * share / (1 - share)))
    convs_used = sorted({meta[q][1] for q in wrong})
    right_ids = sorted(q for q in order if q in right and q not in wrong_by_cls and meta[q][1] in convs_used)
    ctl = pick_controls(wrong, meta, right_ids, n_ctl, rng)
    ids = [q for q in order if q in set(wrong) | set(ctl)]
    cats = Counter(meta[q][0] for q in ids)
    dev, held = set(split["dev_items"]), set(split["heldout_items"])
    used = sorted({meta[q][1] for q in ids})
    sid = f"{name.replace('_', '-')}-s{seed}" + (f"-c{convs}" if convs else "")
    return {
        "benchmark": "locomo",
        "slice_id": sid,
        "conversations": used,
        "kind": "targeted",
        "family": name,
        "source_run": SOURCE_RUN,
        "seed": seed,
        "stratify": (f"all wrong questions of the family (cap {cap}; class floor {floor}, then proportional), controls = correct "
                     f"questions matched on conversation x category (largest remainder, seeded), about {int(share * 100)}% of the slice"),
        "reading_rule": "NOT an estimate of overall accuracy: read target recovered / wrong targets and controls lost / controls",
        "n_total": len(order),
        "n": len(ids),
        "n_target": len(wrong),
        "n_control": len(ctl),
        "sizes": dict(sorted(cats.items())),
        "target_classes": dict(sorted(Counter(wrong_by_cls[q] for q in wrong).items())),
        "conv_policy": "all 10 conversations allowed for the logged full run (owner 2026-10-10); build_forensics_explorer.py "
                       "drops held-out conversations by default",
        "uses_heldout_conversations": bool(set(used) & held),
        "dev_conversations_used": [v for v in used if v in dev],
        "heldout_conversations_used": [v for v in used if v in held],
        "hash": digest(ids),
        "manifest_hash": manifest_hash(ids),
        "category": {q: meta[q][0] for q in ids},
        "target_ids": [q for q in ids if q in set(wrong)],
        "target_class": {q: wrong_by_cls[q] for q in ids if q in set(wrong)},
        "control_ids": [q for q in ids if q in set(ctl)],
        "query_ids": ids,
    }


def slice_name(name: str, seed: int, convs: int | None) -> str:
    return f"fail_slice_{name}_s{seed}" + (f"_c{convs}" if convs else "") + ".json"


def build_all(labels: dict[str, str], order: list[str], meta: dict, right: set[str], split: dict, seed: int = SEED) -> dict[str, dict]:
    """{file name: slice} for every family and the mix, all-conversation and c3 variants."""
    ok = {q: c for q, c in labels.items() if c in FIXABLE and q in meta and q not in right}
    out = {}
    for convs in (None, 3):
        for fam, classes in FAMILIES.items():
            cands = {q: c for q, c in ok.items() if c in classes}
            out[slice_name(fam, seed, convs)] = make_slice(fam, cands, order, meta, right, split, seed, convs)
        out[slice_name(MIX, seed, convs)] = make_slice(
            MIX, ok, order, meta, right, split, seed, convs, cap=MIX_WRONG, floor=CLASS_FLOOR["mix"],
            n_wrong_exact=MIX_WRONG, n_ctl_exact=MIX_WRONG)
    return out


# ------------------------------------------------------------------------------------------------ screen mapping
def derive_families(targets: dict) -> tuple[str, ...]:
    """Families of a screen's PRIMARY target classes (most classes first, at most two); a screen with only secondary
    classes gets the single family holding most of them (one slice, not two: the screen is a weak lever on those)."""
    for key, limit in (("primary", 2), ("secondary", 1)):
        cnt = Counter(f for f in (family_of(c) for c in targets.get(key, [])) if f)
        if cnt:
            return tuple(f for f, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:limit])
    return ()


def read_queue(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        f = line.split("\t")
        if len(f) >= 10:
            rows.append({"id": f[0], "arm": f[3], "rule": f[5], "sets": f[6], "kind": f[8], "est": float(f[9])})
    return rows


def build_map(queue: list[dict], solution_map: dict, seed: int = SEED) -> list[tuple[str, ...]]:
    """Rows (screen, set, slice, slice_gc, note) for the LoCoMo set of every queue row that has a target class family;
    the category-5 rows of ``CAT5_SCREENS`` carry a sixth field ``categories=5``."""
    rows = []
    for q in queue:
        if q["rule"] not in MAP_RULES or not re.search("[fF]", q["sets"]):
            continue
        if q["id"] in CAT5_SCREENS:
            rows.append((q["id"], "loc", CAT5_SLICE.format(seed=seed), CAT5_SLICE_C3.format(seed=seed),
                         f"cat5 (adversarial, abstention); arm {q['arm']}; does the arm still refuse what was never said",
                         CAT5_COLUMN))
        fams = SCREEN_OVERRIDES.get(q["id"]) or derive_families(solution_map["screens"].get(q["id"], {}).get("targets", {}))
        for fam in fams:
            rows.append((q["id"], "loc", f"analysis/{slice_name(fam, seed, None)}", f"analysis/{slice_name(fam, seed, 3)}",
                         f"{fam}; arm {q['arm']}; targets {', '.join(solution_map['screens'].get(q['id'], {}).get('targets', {}).get('primary', [])[:4]) or '(secondary only)'}"))
    return rows


def render_map(rows: list[tuple]) -> str:
    head = ("# screen -> targeted slice (FAIL-SLICE; written by evals/build_targeted_slices.py, read by run_v2_queue.sh in MODE=sample)\n"
            "# columns: screen<TAB>set<TAB>slice (reader-flag rows, kind J)<TAB>slice_gc (engine-LLM rows, kind GC: top-3 conversations)<TAB>note\n"
            "# optional column 6 'categories=5': that row's run passes --categories 5 (LoCoMo category-5 adversarial slices) and its reference run uses the same categories; without it run.sh keeps 1,2,3,4.\n"
            "# A screen with several rows runs once per slice. A screen without a row keeps the random sample slices.\n"
            "# Disable the whole map with V2_SLICE_MAP= (empty); use another file with V2_SLICE_MAP=path. OP-Bench ('o') sets are not mapped.\n")
    return head + "".join("\t".join(r) + "\n" for r in rows)


# ------------------------------------------------------------------------------------------------ GPU estimate
def gpu_estimate(queue: list[dict], rows: list[tuple], slices: dict[str, dict]) -> dict:
    """Planning figures from docs/v2/SCREENS.md: non-GC sample 7 min (80 q), focus 20 min, GC sample about 0.26 x the row's
    focus estimate (39 of 150 min for gc3). A targeted slice of n questions costs 3.5 ingest + 1 start + 1.7 s/q (J)."""
    est = {q["id"]: q for q in queue}
    per_screen: dict[str, dict] = defaultdict(lambda: {"focus": 0.0, "targeted": 0.0, "random_sample": 0.0})
    for sid, _set, sl, slc, _note, *_rest in rows:
        q = est[sid]
        gc = q["kind"] == "GC"
        s = slices.get(Path(slc if gc else sl).name) or json.loads((HERE / (slc if gc else sl)).read_text(encoding="utf-8"))
        t = 0.26 * q["est"] if gc else 3.5 + 1.0 + s["n"] * 1.7 / 60
        per_screen[sid]["targeted"] += t
        per_screen[sid]["focus"] = q["est"]
        per_screen[sid]["random_sample"] = 0.26 * q["est"] if gc else 7.0
    tot = {k: sum(v[k] for v in per_screen.values()) for k in ("focus", "targeted", "random_sample")}
    return {"screens": len(per_screen), "per_screen": dict(per_screen), **tot}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--runs-dir", type=Path, default=None)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out-dir", type=Path, default=HERE / "analysis")
    ap.add_argument("--map-out", type=Path, default=HERE / "v2_slice_map.tsv")
    ap.add_argument("--check", action="store_true", help="rebuild in memory and fail if the committed files differ")
    a = ap.parse_args()
    labels = read_labels(HERE / LABELS)
    order, meta, right = read_universe(find_runs_dir(a.runs_dir))
    split = json.loads((HERE / SPLIT).read_text(encoding="utf-8"))
    slices = build_all(labels, order, meta, right, split, a.seed)
    queue = read_queue(HERE / "v2_queue.tsv")
    rows = build_map(queue, json.loads((HERE / SOLUTION_MAP).read_text(encoding="utf-8")), a.seed)
    files = {a.out_dir / n: json.dumps(s, indent=1) + "\n" for n, s in slices.items()}
    files[a.map_out] = render_map(rows)
    if a.check:
        bad = [str(p) for p, t in files.items() if not p.exists() or p.read_text(encoding="utf-8") != t]
        raise SystemExit(f"stale: {bad}" if bad else 0)
    for p, t in files.items():
        p.write_text(t, encoding="utf-8", newline="\n")
    print(f"{'slice':44s} {'wrong':>5s} {'ctl':>4s} {'n':>4s} convs  heldout")
    for n, s in slices.items():
        print(f"{n[:-5]:44s} {s['n_target']:5d} {s['n_control']:4d} {s['n']:4d} {len(s['conversations']):5d}  {s['uses_heldout_conversations']}")
    e = gpu_estimate(queue, rows, slices)
    print(f"mapped screens {e['screens']}: focus {e['focus']:.0f} min, random sample {e['random_sample']:.0f} min, targeted {e['targeted']:.0f} min")


if __name__ == "__main__":
    main()
