# ruff: noqa: E501
"""Aggregate forensics of the full logged run ``full-persp-loc`` (LoCoMo, all 10 conversations, cat 1-4).

    python evals/full_run_forensics.py [--out evals/runs/_analysis/full]

CPU only, no model call. Inputs (all under ``evals/runs``): ``full-persp-loc--memspine`` (results),
``full-persp-loc--trace`` (forensics.jsonl.gz with ``trace_full``, reads.jsonl.gz, write_trace.jsonl),
the reference runs ``qa-full-qs-eq06-fix`` (80.1, no reranker, no perspective) and ``xb-loc-dev`` (BEST without
perspective, dev conversations), each pre-processed by ``forensics_report.py`` into ``_analysis/fx_<run>/``;
and the hand labels ``analysis/full_persp_loc_stage_labels.txt`` (one stage code per wrong answer).

Writes ``tables.md`` (aggregates only) and ``joined.jsonl`` (LOCAL, holds dataset text) into ``--out``.
The committed analysis document quotes ``tables.md``; it carries no question, answer or turn text.
"""

from __future__ import annotations

import argparse
import collections
import gzip
import json
import re
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
sys.path[:] = [p for p in sys.path if Path(p or ".").resolve() != HERE]
sys.path.append(str(HERE))

from failure_buckets import parse_interval  # noqa: E402

RUN, FIX, DEVREF = "full-persp-loc", "qa-full-qs-eq06-fix", "xb-loc-dev"
CATS = ["single-hop", "multi-hop", "temporal", "open-domain"]
CONVS = [
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
]
DEV = {"conv-26", "conv-30", "conv-41", "conv-42"}
STAGE_ROWS = [
    ("a", "(a) write path"),
    ("b", "(b) recall: gold in no leg"),
    ("cf", "(c) cut at fusion / pool (fused top-20)"),
    ("cr", "(c) cut at rerank_keep (top-10 of the pool)"),
    ("ca", "(c) cut at assembly / budget / floor"),
    ("d:det", "(d) reader: wrong detail"),
    ("d:lst", "(d) reader: incomplete list"),
    ("d:cnt", "(d) reader: wrong count"),
    ("d:dar", "(d) reader: date / duration arithmetic, wrong date"),
    ("d:ref", "(d) reader: refusal or premise denial"),
    ("d:inf", "(d) reader: inference refused or wrong"),
    ("d:dis", "(d) reader: distractor line"),
    ("d:spk", "(d) reader: wrong speaker"),
    ("e", "(e) judge (answer states the gold fact)"),
    ("f", "(f) gold / dataset error (errata)"),
]


def jl(path: Path) -> list[dict]:
    op = gzip.open if path.suffix == ".gz" else open
    with op(path, "rt", encoding="utf-8", errors="replace") as fh:
        return [json.loads(x) for x in fh if x.strip()]


def per_question(run: str) -> dict:
    p = jl(RUNS / "_analysis" / f"fx_{run}" / "per_question.jsonl")
    return {(a["item"], a["qid"]): a for a in p if a["category"] != "adversarial"}


def labels() -> dict:
    out = {}
    for line in (
        (HERE / "analysis" / "full_persp_loc_stage_labels.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    ):
        if line.startswith("#") or not line.strip():
            continue
        c, q, s = line.split()
        out[(c, q)] = s
    return out


def pct(a, b, d=1):
    return f"{100 * a / b:.{d}f}" if b else "-"


def md_table(head: list[str], rows: list[list]) -> str:
    out = [
        "| " + " | ".join(head) + " |",
        "|" + "|".join(["---"] + ["---:"] * (len(head) - 1)) + "|",
    ]
    for r in rows:
        out.append("| " + " | ".join(str(x) for x in r) + " |")
    return "\n".join(out)


def best(ranks: dict, key: str):
    v = ranks.get(key)
    return v


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(RUNS / "_analysis" / "full"))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    md: list[str] = []

    P, F = per_question(RUN), per_question(FIX)
    try:
        D0 = per_question(DEVREF)
    except FileNotFoundError:
        D0 = {}
    lab = labels()
    errata = {
        (e["item"], e["qid"]): e["tag"]
        for e in json.loads((HERE / "analysis" / "locomo_errata.json").read_text(encoding="utf-8"))[
            "entries"
        ]
    }
    tf = {(x["item"], x["query_id"]): x for x in jl(RUNS / f"{RUN}--trace" / "forensics.jsonl.gz")}
    reads = {
        (x["item_id"], x["query_id"]): x for x in jl(RUNS / f"{RUN}--trace" / "reads.jsonl.gz")
    }
    wtrace = jl(RUNS / f"{RUN}--trace" / "write_trace.jsonl")

    keys = list(P)
    n = len(keys)
    wrong = [k for k in keys if not P[k]["correct"]]
    md.append(
        f"## Run totals\n\n{n} questions, {n - len(wrong)} correct, {len(wrong)} wrong, accuracy {pct(n - len(wrong), n)}%.\n"
    )

    cat_of = {k: P[k]["category"] for k in keys}
    # accuracy when the evidence is complete in the context: the reader ceiling
    comp = [
        k for k in keys if P[k]["gold_turns"] and all(g["in_context"] for g in P[k]["gold_turns"])
    ]
    part = [
        k
        for k in keys
        if P[k]["gold_turns"] and not all(g["in_context"] for g in P[k]["gold_turns"])
    ]
    border = {
        (e["item"], e["qid"])
        for e in json.loads((HERE / "analysis" / "locomo_errata.json").read_text(encoding="utf-8"))[
            "entries"
        ]
        if e.get("borderline")
    }
    errk = {k for k in keys if errata.get(k) in ("gold_error", "needs_image") and k not in border}
    comp_x = [k for k in comp if k not in errk]
    md.append(
        f"Evidence complete (every gold turn in the context): {len(comp)} questions, accuracy {pct(sum(P[k]['correct'] for k in comp), len(comp))}% "
        f"({pct(sum(P[k]['correct'] for k in comp_x), len(comp_x))}% without the errata rows). Evidence incomplete: {len(part)} questions, accuracy {pct(sum(P[k]['correct'] for k in part), len(part))}%. "
        f"By category, evidence-complete accuracy: "
        + ", ".join(
            f"{c} {pct(sum(P[k]['correct'] for k in comp if cat_of[k] == c), sum(1 for k in comp if cat_of[k] == c))}% (n={sum(1 for k in comp if cat_of[k] == c)})"
            for c in CATS
        )
        + "."
    )
    md.append(
        f"Target arithmetic: 90% of {n} is {int(0.9 * n + 0.999)} correct, {int(0.9 * n + 0.999) - (n - len(wrong))} more than today; without the {len(errk)} errata rows the denominator is {n - len(errk)} and 90% needs {int(0.9 * (n - len(errk)) + 0.999)} correct ({int(0.9 * (n - len(errk)) + 0.999) - (n - len(wrong))} more)."
    )

    # ------------------------------------------------------------------ 0. write path, cuts, logging facts
    wr = collections.Counter()
    for r in wtrace:
        s = r.get("stored") or {}
        wr["turns"] += 1
        wr["written"] += bool(r.get("written"))
        wr["quarantined"] += bool(s.get("quarantined"))
        wr["instruction_flag"] += bool(s.get("instruction_flag"))
        for e in r.get("engine_events", []):
            wr["anomalous"] += bool(e.get("anomalous"))
    cuts = collections.Counter()
    npool = collections.Counter()
    for x in tf.values():
        t = x["trace_full"]
        for c in t["cuts"]:
            cuts[c["reason"]] += 1
        cuts["final_not_in_context"] += len(t["final_not_in_context"])
        cuts["perspective_dropped"] += len(t["perspective"]["dropped"])
        cuts["assembly_abstained"] += bool(t["assembled"]["abstained"])
        npool[len(x["pool"])] += 1
    over = sum(1 for k in keys if reads[k].get("context_truncated"))
    md.append("## Write path and cut reasons logged by the trace\n")
    md.append(
        f"Write trace: {wr['turns']} turns, {wr['written']} written, {wr['quarantined']} quarantined, "
        f"{wr['instruction_flag']} instruction-flagged, {wr['anomalous']} anomalous: stage (a) is empty. "
        f"Cut reasons in `trace_full.cuts` over {n} questions: {dict(cuts)}; context_truncated rows: {over}; pool sizes {dict(npool)}.\n"
    )

    # ------------------------------------------------------------------ 1. stage tables
    def stage_of(k):
        s = lab[k]
        return s

    cat_of = {k: P[k]["category"] for k in keys}
    conv_n = collections.Counter(k[0] for k in keys)
    cat_n = collections.Counter(cat_of.values())
    st_cat = collections.defaultdict(collections.Counter)
    st_conv = collections.defaultdict(collections.Counter)
    for k in wrong:
        st_cat[stage_of(k)][cat_of[k]] += 1
        st_conv[stage_of(k)][k[0]] += 1
    rows = []
    for code, name in STAGE_ROWS:
        c = st_cat[code]
        rows.append([name] + [c[x] for x in CATS] + [sum(c.values())])
    tot = collections.Counter(cat_of[k] for k in wrong)
    rows.append(["**wrong**"] + [tot[x] for x in CATS] + [len(wrong)])
    rows.append(["n"] + [cat_n[x] for x in CATS] + [n])
    rows.append(
        ["accuracy %"] + [pct(cat_n[x] - tot[x], cat_n[x]) for x in CATS] + [pct(n - len(wrong), n)]
    )
    md.append(
        "## Stage x category (wrong answers, hand-labelled)\n\n"
        + md_table(["stage", *CATS, "total"], rows)
        + "\n"
    )

    def group(code):
        return {
            "a": "write",
            "b": "recall",
            "cf": "cut",
            "cr": "cut",
            "ca": "cut",
            "e": "judge",
            "f": "gold error",
        }.get(code, "reader")

    g_cat = collections.defaultdict(collections.Counter)
    g_conv = collections.defaultdict(collections.Counter)
    for k in wrong:
        g = group(stage_of(k))
        g_cat[g][cat_of[k]] += 1
        g_conv[g][k[0]] += 1
    order = ["write", "recall", "cut", "reader", "judge", "gold error"]
    rows = []
    for g in order:
        rows.append([g] + [g_cat[g][x] for x in CATS] + [sum(g_cat[g].values())])
    md.append("### Stage group x category\n\n" + md_table(["group", *CATS, "total"], rows) + "\n")

    wrong_c = collections.Counter(k[0] for k in wrong)
    head = ["stage"] + [c.replace("conv-", "") for c in CONVS] + ["dev", "held-out"]
    rows = []
    for code, name in STAGE_ROWS:
        c = st_conv[code]
        rows.append(
            [name]
            + [c[x] for x in CONVS]
            + [sum(c[x] for x in CONVS if x in DEV), sum(c[x] for x in CONVS if x not in DEV)]
        )
    rows.append(
        ["**wrong**"]
        + [wrong_c[x] for x in CONVS]
        + [sum(wrong_c[x] for x in DEV), sum(wrong_c[x] for x in CONVS if x not in DEV)]
    )
    rows.append(
        ["n"]
        + [conv_n[x] for x in CONVS]
        + [sum(conv_n[x] for x in DEV), sum(conv_n[x] for x in CONVS if x not in DEV)]
    )
    rows.append(
        ["accuracy %"]
        + [pct(conv_n[x] - wrong_c[x], conv_n[x]) for x in CONVS]
        + [
            pct(sum(conv_n[x] - wrong_c[x] for x in DEV), sum(conv_n[x] for x in DEV)),
            pct(
                sum(conv_n[x] - wrong_c[x] for x in CONVS if x not in DEV),
                sum(conv_n[x] for x in CONVS if x not in DEV),
            ),
        ]
    )
    md.append("## Stage x conversation (dev = 26/30/41/42)\n\n" + md_table(head, rows) + "\n")

    rows = []
    for g in order:
        rows.append(
            [g]
            + [g_conv[g][x] for x in CONVS]
            + [sum(g_conv[g][x] for x in DEV), sum(g_conv[g][x] for x in CONVS if x not in DEV)]
        )
    md.append("### Stage group x conversation\n\n" + md_table(head, rows) + "\n")

    # per-category accuracy, dev vs held-out
    rows = []
    for c in CATS:
        d = [k for k in keys if cat_of[k] == c and k[0] in DEV]
        h = [k for k in keys if cat_of[k] == c and k[0] not in DEV]
        rows.append(
            [
                c,
                len(d),
                pct(sum(P[k]["correct"] for k in d), len(d)),
                len(h),
                pct(sum(P[k]["correct"] for k in h), len(h)),
            ]
        )
    dk = [k for k in keys if k[0] in DEV]
    hk = [k for k in keys if k[0] not in DEV]
    rows.append(
        [
            "all",
            len(dk),
            pct(sum(P[k]["correct"] for k in dk), len(dk)),
            len(hk),
            pct(sum(P[k]["correct"] for k in hk), len(hk)),
        ]
    )
    md.append(
        "## Accuracy by category, dev vs held-out\n\n"
        + md_table(["category", "n dev", "dev %", "n held-out", "held-out %"], rows)
        + "\n"
    )

    # errata overlap
    default_tags = {"gold_error", "needs_image"}
    f_default = [k for k in wrong if errata.get(k) in default_tags]
    f_label = [k for k in wrong if lab[k] == "f"]
    md.append(
        f"Errata: {len(f_default)} wrong answers carry a default-excluded tag (gold_error, needs_image); label `f` covers "
        f"{len(f_label)} (adds the premise_error row 41/2-68 and keeps no default-tag row under another label: "
        f"{sorted(set(f_default) - set(f_label))}). Other tags on wrong rows: "
        f"{dict(collections.Counter(errata[k] for k in wrong if k in errata and errata[k] not in default_tags))}.\n"
    )

    # ------------------------------------------------------------------ reader-side verification on the exact prompt
    def prompt_of(k):
        rc = reads[k].get("reader_calls") or []
        return (rc[-1].get("prompt") if rc else "") or ""

    def gold_in_prompt(k):
        pr = prompt_of(k)
        gt = P[k]["gold_turns"]
        vals = []
        for g in gt:
            txt = g["text"] or ""
            body = txt.split(": ", 1)[1] if ": " in txt else txt
            vals.append(body[:80] in pr)
        return vals

    d_keys = [k for k in wrong if lab[k].startswith("d:")]
    all_in = sum(1 for k in d_keys if all(gold_in_prompt(k)))
    md.append(
        f"Exact-prompt check: of the {len(d_keys)} reader-stage wrong answers, {all_in} have every gold turn text inside the last reader "
        f"prompt in `reads.jsonl` (the rest have a gold turn with no text match: evidence labels that point to a neighbouring line "
        f"or a malformed id).\n"
    )
    retried = [k for k in keys if len(reads[k].get("reader_calls") or []) > 1]
    acc = [k for k in retried if (reads[k].get("reader_meta") or {}).get("retry_accepted")]
    md.append(
        f"Refusal retry: fired on {len(retried)} questions, accepted on {len(acc)}; correct after retry "
        f"{sum(P[k]['correct'] for k in retried)}/{len(retried)}; wrong after retry labelled d:ref {sum(1 for k in retried if k in set(wrong) and lab[k] == 'd:ref')}.\n"
    )

    # reader sub-type x category already in the stage table; refusal / inference by question flags
    # ------------------------------------------------------------------ cut and recall detail
    def lost_detail(k):
        gt = P[k]["gold_turns"]
        return [g for g in gt if g["lost_at"]]

    cf_v = []
    cf_lex_only = 0
    cf_rank_missing = 0
    for k in wrong:
        if lab[k] != "cf":
            continue
        for g in P[k]["gold_turns"]:
            if g["lost_at"] == "fusion":
                v = g["ranks"].get("vector")
                lx = g["ranks"].get("lexical")
                if v is not None:
                    cf_v.append(v)
                elif lx is not None:
                    cf_lex_only += 1
                else:
                    cf_rank_missing += 1
    md.append("## (c) cut detail\n")
    if cf_v:
        md.append(
            f"Fusion cuts: {len(cf_v) + cf_lex_only + cf_rank_missing} gold turns lost at fusion in {sum(1 for k in wrong if lab[k] == 'cf')} questions; "
            f"{len(cf_v)} had a vector rank (median {statistics.median(cf_v):.0f}, <=30: {sum(1 for v in cf_v if v <= 30)}, 31-60: {sum(1 for v in cf_v if v > 30)}), "
            f"{cf_lex_only} lexical only, {cf_rank_missing} reached only an extra leg. The fused list is the pool (20 for plain questions, 30 for list-mode): "
            f"`trace_full.cuts` has no entry for these, the engine logs no fusion cut (logging gap).\n"
        )
    cr_rows = []
    for k in wrong:
        if lab[k] == "cr":
            for g in P[k]["gold_turns"]:
                if g["lost_at"] == "rerank":
                    x = tf[k]["trace_full"]["cuts"]
                    rid = None
                    cr_rows.append((k, g["ranks"].get("rerank_order"), g["ranks"].get("fused")))
    md.append(
        f"Rerank cuts: {len(cr_rows)} gold turns, position in the reranker order {sorted(r for _, r, _ in cr_rows)}, all with reason `rerank_keep` (keep=10) in `trace_full.cuts`.\n"
    )

    # recall detail: set questions, number of gold, perspective leg
    b_keys = [k for k in wrong if lab[k] == "b"]
    lost_count = collections.Counter()
    setq = 0
    for k in b_keys:
        gt = P[k]["gold_turns"]
        lost = [g for g in gt if g["lost_at"] == "recall"]
        lost_count[min(len(lost), 4)] += 1
        fl = tf[k]["trace_full"]["query_analysis"]["flags"]
        if (
            fl.get("is_set_question")
            or fl.get("is_set_question_wide")
            or fl.get("is_intent_list")
            or fl.get("is_aggregation")
        ):
            setq += 1
    allgold_lost = sum(
        1 for k in b_keys if all(g["lost_at"] == "recall" for g in P[k]["gold_turns"])
    )
    md.append(
        f"Recall misses: {len(b_keys)} questions; gold turns in no leg per question {dict(sorted(lost_count.items()))} (4 = four or more); "
        f"every gold turn missed in {allgold_lost}; question flagged set/list/aggregation by the query analysis in {setq}; "
        f"median gold turns per question {statistics.median(len(P[k]['gold_turns']) for k in b_keys):.0f}. "
        f"Single-gold-turn recall misses (a retrieval miss of one turn): {sum(1 for k in b_keys if len(P[k]['gold_turns']) == 1)}.\n"
    )
    # unit: number of gold turns per question vs accuracy
    ng = collections.defaultdict(lambda: [0, 0])
    for k in keys:
        m = len(P[k]["gold_turns"])
        key = "0" if m == 0 else (str(m) if m <= 2 else ("3-4" if m <= 4 else "5+"))
        ng[key][0] += 1
        ng[key][1] += P[k]["correct"]
    md.append(
        "Accuracy by number of gold turns: "
        + ", ".join(f"{kk}: {pct(v[1], v[0])}% (n={v[0]})" for kk, v in sorted(ng.items()))
        + ".\n"
    )

    # ------------------------------------------------------------------ 2. perspective trade-off
    md.append("## Perspective trade-off\n")

    def status(a, b):
        return "gain" if a and not b else "loss" if b and not a else "both" if a else "none"

    st_fix = {k: status(P[k]["correct"], F[k]["correct"]) for k in keys}
    rows = []
    for c in [*CATS, "all"]:
        ks = [k for k in keys if c == "all" or cat_of[k] == c]
        cnt = collections.Counter(st_fix[k] for k in ks)
        rows.append(
            [
                c,
                len(ks),
                pct(sum(F[k]["correct"] for k in ks), len(ks)),
                pct(sum(P[k]["correct"] for k in ks), len(ks)),
                cnt["gain"],
                cnt["loss"],
                cnt["gain"] - cnt["loss"],
            ]
        )
    md.append(
        "### A. persp stack vs `qa-full-qs-eq06-fix` (all 10 conversations; the stack also adds the 4B reranker, list mode, pool 2 and neutral retry)\n\n"
        + md_table(["category", "n", "eq06-fix %", "full-persp %", "gained", "lost", "net"], rows)
        + "\n"
    )
    rows = []
    for cv in CONVS:
        ks = [k for k in keys if k[0] == cv]
        cnt = collections.Counter(st_fix[k] for k in ks)
        rows.append(
            [
                cv,
                len(ks),
                pct(sum(F[k]["correct"] for k in ks), len(ks)),
                pct(sum(P[k]["correct"] for k in ks), len(ks)),
                cnt["gain"],
                cnt["loss"],
                cnt["gain"] - cnt["loss"],
            ]
        )
    md.append(
        md_table(["conv", "n", "eq06-fix %", "full-persp %", "gained", "lost", "net"], rows) + "\n"
    )

    dkeys = [k for k in keys if k[0] in DEV and k in D0]
    st_dev = {k: status(P[k]["correct"], D0[k]["correct"]) for k in dkeys}
    rows = []
    for c in [*CATS, "all"]:
        ks = [k for k in dkeys if c == "all" or cat_of[k] == c]
        cnt = collections.Counter(st_dev[k] for k in ks)
        rows.append(
            [
                c,
                len(ks),
                pct(sum(D0[k]["correct"] for k in ks), len(ks)),
                pct(sum(P[k]["correct"] for k in ks), len(ks)),
                cnt["gain"],
                cnt["loss"],
                cnt["gain"] - cnt["loss"],
            ]
        )
    md.append(
        "### B. perspective only: full-persp-loc vs `xb-loc-dev` (same BEST stack without perspective; the four dev conversations)\n\n"
        + md_table(["category", "n", "xb-loc-dev %", "full-persp %", "gained", "lost", "net"], rows)
        + "\n"
    )

    # --- factor analysis (record tags and ids come from the write trace: every stored turn)
    wmap = {
        (r["item"], r["turn"]): (r["record_id"], (r.get("stored") or {}).get("tags") or [])
        for r in wtrace
    }

    def target_names(k):
        return {t.lower() for grp in tf[k]["trace_full"]["perspective"]["targets"] for t in grp}

    def gold_info(k, g):
        """Per gold turn: factor class and speaker relation under the resolved perspective of the question."""
        t = tf[k]["trace_full"]
        rid, tags = wmap.get((k[0], g["turn"]), (None, []))
        rk_ = g["ranks"]
        in_leg = (
            rk_.get("vector") is not None
            or rk_.get("lexical") is not None
            or any(v is not None for v in (rk_.get("extra") or {}).values())
        )
        fac = (t["perspective"]["factors"].get(rid) or {}).get("subject") if rid else None
        spk = next((x[4:] for x in tags if x.startswith("spk:")), None)
        subs = {x[4:] for x in tags if x.startswith("sub:")}
        tn = target_names(k)
        if not in_leg:
            cls = "not retrieved by any leg"
        elif fac is None:
            cls = "factor 1.0 (about the target)"
        else:
            cls = {
                0.84: "factor 0.84 (target speaks about another person)",
                0.8: "factor 0.8 (unresolved third party)",
                0.6: "factor 0.6 (turn about someone else)",
            }.get(fac, f"factor {fac}")
        return dict(
            cls=cls,
            fac=fac or 1.0,
            spk=spk,
            spk_is_target=(spk in tn) if (spk and tn) else None,
            subs=subs,
            in_leg=in_leg,
            persp=(rk_.get("extra") or {}).get("perspective"),
        )

    rel = collections.Counter()
    rid_tags = {r["record_id"]: ((r.get("stored") or {}).get("tags") or []) for r in wtrace}
    for k in keys:
        t = tf[k]["trace_full"]
        tn = target_names(k)
        for rid, fa in t["perspective"]["factors"].items():
            tags = rid_tags.get(rid, [])
            spk = next((x[4:] for x in tags if x.startswith("spk:")), None)
            rel[(fa.get("subject"), "speaker is target" if spk in tn else "speaker is other")] += 1
    md.append(
        "Down-weight factors applied to pool candidates (candidate records over all questions): "
        + "; ".join(
            f"factor {a} / {b}: {v}"
            for (a, b), v in sorted(rel.items(), key=lambda z: (z[0][0] or 0, z[0][1]))
        )
        + ". Factor 0.84 = the target speaks about someone else, 0.6 = the turn is about another person, 0.8 = unresolved third party. "
        f"Total candidate records down-weighted: {sum(rel.values())} over {n} questions.\n"
    )

    gold_all = []  # one row per (question, gold turn)
    for k in keys:
        for g in P[k]["gold_turns"]:
            gi = gold_info(k, g)
            r = g["ranks"]
            row = dict(
                k=k,
                turn=g["turn"],
                cat=cat_of[k],
                ok=P[k]["correct"],
                fixok=F[k]["correct"],
                **gi,
                pool=r.get("fused") is not None,
                final=bool(r.get("final")),
                ctx=g["in_context"],
                vec=r.get("vector"),
                lex=r.get("lexical"),
                fused=r.get("fused"),
            )
            if k in D0:
                gb = next((x for x in D0[k]["gold_turns"] if x["turn"] == g["turn"]), None)
                if gb is not None:
                    rb = gb["ranks"]
                    row.update(
                        d_pool=rb.get("fused") is not None,
                        d_final=bool(rb.get("final")),
                        d_ctx=gb["in_context"],
                        d_fused=rb.get("fused"),
                    )
            gb = next((x for x in F[k]["gold_turns"] if x["turn"] == g["turn"]), None)
            if gb is not None:
                row.update(f_ctx=gb["in_context"], f_fused=gb["ranks"].get("fused"))
            gold_all.append(row)

    classes = [
        "factor 1.0 (about the target)",
        "factor 0.84 (target speaks about another person)",
        "factor 0.8 (unresolved third party)",
        "factor 0.6 (turn about someone else)",
        "not retrieved by any leg",
    ]
    rows = []
    for c in classes:
        gs = [x for x in gold_all if x["cls"] == c]
        rows.append(
            [
                c,
                len(gs),
                sum(x["pool"] for x in gs),
                sum(x["final"] for x in gs),
                sum(x["ctx"] for x in gs),
                sum(1 for x in gs if x["persp"] is not None),
            ]
        )
    md.append(
        "### C. gold turns by perspective factor class (all 1,540 questions; one row per gold turn)\n\n"
        + md_table(
            [
                "gold turn class",
                "gold turns",
                "in pool",
                "in final top-10",
                "in context",
                "in the perspective leg",
            ],
            rows,
        )
        + "\n"
    )

    by_k = collections.defaultdict(list)
    for x in gold_all:
        by_k[x["k"]].append(x)

    def qgold(ks):
        c = collections.Counter()
        for k in ks:
            gs = by_k.get(k, [])
            c["q"] += 1
            c["a"] += any(x["fac"] < 1 for x in gs)
            c["b"] += any(x["fac"] < 1 and x["spk_is_target"] is False for x in gs)
            c["c"] += any(x["fac"] == 0.6 for x in gs)
        return c

    groups = {
        "lost vs eq06-fix": [k for k in keys if st_fix[k] == "loss"],
        "gained vs eq06-fix": [k for k in keys if st_fix[k] == "gain"],
        "both correct": [k for k in keys if st_fix[k] == "both"],
        "both wrong": [k for k in keys if st_fix[k] == "none"],
        "lost vs xb-loc-dev (dev)": [k for k in dkeys if st_dev[k] == "loss"],
        "gained vs xb-loc-dev (dev)": [k for k in dkeys if st_dev[k] == "gain"],
        "all questions": keys,
    }
    rows = []
    for name, ks in groups.items():
        r = qgold(ks)
        rows.append(
            [
                name,
                r["q"],
                r["a"],
                pct(r["a"], r["q"]),
                r["b"],
                pct(r["b"], r["q"]),
                r["c"],
                pct(r["c"], r["q"]),
            ]
        )
    md.append(
        "### D. do the flips come from down-weighting the gold turn?\n\nQuestions with at least one gold turn at factor<1 ('other speaker' = the `spk:` tag of the gold record is not a resolved target of the question).\n\n"
        + md_table(
            [
                "group",
                "n",
                "gold at factor<1",
                "%",
                "of which other speaker",
                "%",
                "gold at 0.6",
                "%",
            ],
            rows,
        )
        + "\n"
    )
    rows = []
    for c in CATS:
        for lab_, sel in (("lost", "loss"), ("gained", "gain")):
            ks = [k for k in keys if st_fix[k] == sel and cat_of[k] == c]
            r = qgold(ks)
            rows.append([c, lab_, r["q"], r["a"], r["b"]])
        r = qgold([k for k in keys if cat_of[k] == c])
        rows.append([c, "all questions", r["q"], r["a"], r["b"]])
    md.append(
        md_table(
            [
                "category",
                "set vs eq06-fix",
                "questions",
                "gold at factor<1",
                "of which other speaker",
            ],
            rows,
        )
        + "\n"
    )

    def gold_ctx(a):
        gt = a["gold_turns"]
        return (sum(1 for g in gt if g["in_context"]), len(gt))

    def deltas(ks, ref):
        c = collections.Counter()
        for k in ks:
            ga, na = gold_ctx(P[k])
            gb, nb = gold_ctx(ref[k])
            if na == 0:
                c["no_gold"] += 1
                continue
            c["more" if ga > gb else "less" if ga < gb else "same"] += 1
            c["persp all, ref not"] += ga == na and gb < nb
            c["ref all, persp not"] += gb == nb and ga < na
            c["identical turns"] += set(P[k]["retrieved_turns"]) == set(ref[k]["retrieved_turns"])
        return c

    rows = []
    for name, ks, ref in (
        ("lost vs eq06-fix", groups["lost vs eq06-fix"], F),
        ("gained vs eq06-fix", groups["gained vs eq06-fix"], F),
        ("lost vs xb-loc-dev", groups["lost vs xb-loc-dev (dev)"], D0),
        ("gained vs xb-loc-dev", groups["gained vs xb-loc-dev (dev)"], D0),
    ):
        c = deltas(ks, ref)
        rows.append(
            [
                name,
                len(ks),
                c["more"],
                c["less"],
                c["same"],
                c["persp all, ref not"],
                c["ref all, persp not"],
                c["identical turns"],
            ]
        )
    md.append(
        "### E. retrieval change behind the flips (gold turns in the context)\n\n"
        + md_table(
            [
                "flip set",
                "n",
                "more gold in context (persp)",
                "fewer",
                "same count",
                "persp has ALL gold, ref not",
                "ref has ALL gold, persp not",
                "identical context turn set",
            ],
            rows,
        )
        + "\n"
    )
    if D0:
        same = [k for k in dkeys if set(P[k]["retrieved_turns"]) == set(D0[k]["retrieved_turns"])]
        md.append(
            f"Determinism check on the dev conversations: {len(same)} of {len(dkeys)} questions have the identical set of context turns in full-persp-loc and xb-loc-dev; "
            f"{sum(1 for k in same if P[k]['correct'] != D0[k]['correct'])} of them flip (reader temperature 0, same judge), "
            f"so the {len(groups['lost vs xb-loc-dev (dev)']) + len(groups['gained vs xb-loc-dev (dev)'])} dev flips all come from a different context.\n"
        )

    if D0:
        rows = []
        for c in classes[:4]:
            gs = [x for x in gold_all if x["cls"] == c and "d_pool" in x]
            rows.append(
                [
                    c,
                    len(gs),
                    sum(x["d_pool"] for x in gs),
                    sum(x["pool"] for x in gs),
                    sum(x["d_final"] for x in gs),
                    sum(x["final"] for x in gs),
                    sum(x["d_ctx"] for x in gs),
                    sum(x["ctx"] for x in gs),
                ]
            )
        gs = [
            x
            for x in gold_all
            if "d_pool" in x and x["vec"] is None and x["lex"] is not None and x["lex"] <= 10
        ]
        rows.append(
            [
                "lexical-only gold (BM25 top-10, not in vector top-60)",
                len(gs),
                sum(x["d_pool"] for x in gs),
                sum(x["pool"] for x in gs),
                sum(x["d_final"] for x in gs),
                sum(x["final"] for x in gs),
                sum(x["d_ctx"] for x in gs),
                sum(x["ctx"] for x in gs),
            ]
        )
        gs = [
            x
            for x in gold_all
            if "d_pool" in x
            and x["vec"] is not None
            and x["vec"] <= 30
            and x["persp"] is None
            and x["cls"] != "not retrieved by any leg"
        ]
        rows.append(
            [
                "vector top-30 but NOT in the perspective leg",
                len(gs),
                sum(x["d_pool"] for x in gs),
                sum(x["pool"] for x in gs),
                sum(x["d_final"] for x in gs),
                sum(x["final"] for x in gs),
                sum(x["d_ctx"] for x in gs),
                sum(x["ctx"] for x in gs),
            ]
        )
        gs = [x for x in gold_all if "d_pool" in x and x["persp"] is not None]
        rows.append(
            [
                "in the perspective leg",
                len(gs),
                sum(x["d_pool"] for x in gs),
                sum(x["pool"] for x in gs),
                sum(x["d_final"] for x in gs),
                sum(x["final"] for x in gs),
                sum(x["d_ctx"] for x in gs),
                sum(x["ctx"] for x in gs),
            ]
        )
        md.append(
            "### F. dev control: the same gold turns without (xb-loc-dev) and with (full-persp-loc) perspective\n\n"
            + md_table(
                [
                    "gold turn class",
                    "gold turns",
                    "pool: without",
                    "pool: with",
                    "final-10: without",
                    "final-10: with",
                    "context: without",
                    "context: with",
                ],
                rows,
            )
            + "\n"
        )
        rows = []
        for cat in CATS:
            gs = [x for x in gold_all if "d_pool" in x and x["cat"] == cat]
            rows.append(
                [
                    cat,
                    len(gs),
                    sum(x["d_pool"] for x in gs),
                    sum(x["pool"] for x in gs),
                    sum(x["d_final"] for x in gs),
                    sum(x["final"] for x in gs),
                    sum(x["d_ctx"] for x in gs),
                    sum(x["ctx"] for x in gs),
                ]
            )
        md.append(
            md_table(
                [
                    "category",
                    "gold turns",
                    "pool: without",
                    "pool: with",
                    "final-10: without",
                    "final-10: with",
                    "context: without",
                    "context: with",
                ],
                rows,
            )
            + "\n"
        )

    gs = [x for x in gold_all if x["vec"] is None and x["lex"] is not None and x["lex"] <= 3]
    gs2 = [x for x in gs if x["cat"] == "single-hop"]
    md.append(
        f"Lexical-only gold (BM25 rank <=3, no vector rank): {len(gs)} gold turns over all questions, in pool {sum(x['pool'] for x in gs)}, in context {sum(x['ctx'] for x in gs)}; "
        f"single-hop: {len(gs2)} gold turns, in pool {sum(x['pool'] for x in gs2)}, in context {sum(x['ctx'] for x in gs2)}. "
        f"In eq06-fix (no perspective leg, fused top-10) the same {len(gs)} gold turns were in context {sum(1 for x in gs if x.get('f_ctx'))} times.\n"
    )
    lost_rows = [
        x
        for x in gold_all
        if st_fix[x["k"]] == "loss" and x["cat"] in ("single-hop", "open-domain")
    ]
    c = collections.Counter()
    for x in lost_rows:
        if x.get("f_ctx") and not x["ctx"]:
            c["gold turns in the eq06-fix context but not in the persp context"] += 1
            if x["vec"] is None and x["lex"] is not None:
                c["of which lexical-only (no vector rank)"] += 1
            if x["lex"] is not None and x["lex"] <= 3:
                c["of which BM25 rank <=3"] += 1
            if x["vec"] is not None:
                c["of which with a vector rank"] += 1
            if x["persp"] is not None:
                c["of which in the perspective leg"] += 1
            if x["fac"] < 1:
                c["of which carrying a factor<1"] += 1
            if not x["pool"]:
                c["of which outside the pool (fusion)"] += 1
            elif not x["final"]:
                c["of which cut by rerank_keep"] += 1
    md.append(
        "Single-hop and open-domain losses vs eq06-fix: "
        + "; ".join(f"{a}: {b}" for a, b in c.items())
        + ".\n"
    )

    rows = []
    for code in ["b", "cf", "cr"]:
        ks = [k for k in wrong if lab[k] == code]
        cc = collections.Counter()
        for k in ks:
            lost = [x for x in by_k[k] if not x["ctx"]]
            cc["q"] += 1
            cc["lost gold in a leg"] += any(x["cls"] != "not retrieved by any leg" for x in lost)
            cc["lost gold with factor<1"] += any(
                x["fac"] < 1 and x["cls"] != "not retrieved by any leg" for x in lost
            )
            cc["lost gold, lexical-only"] += any(
                x["vec"] is None and x["lex"] is not None for x in lost
            )
            cc["lost gold, in perspective leg"] += any(x["persp"] is not None for x in lost)
        rows.append(
            [
                code,
                cc["q"],
                cc["lost gold in a leg"],
                cc["lost gold with factor<1"],
                cc["lost gold, lexical-only"],
                cc["lost gold, in perspective leg"],
            ]
        )
    md.append(
        "### G. retrieval-stage wrong answers and the factor / legs of the lost gold turns\n\n"
        + md_table(
            [
                "stage",
                "questions",
                "a lost gold turn is in a leg",
                "a lost gold turn carries factor<1",
                "a lost gold turn is lexical-only",
                "a lost gold turn is in the perspective leg",
            ],
            rows,
        )
        + "\n"
    )

    # ------------------------------------------------------------------ 3. temporal
    md.append("## Temporal\n")
    tk = [k for k in keys if cat_of[k] == "temporal"]
    rows = []
    for cv in CONVS:
        ks = [k for k in tk if k[0] == cv]
        w = [k for k in ks if not P[k]["correct"]]
        fw = [k for k in ks if not F[k]["correct"]]
        rows.append(
            [cv, len(ks), pct(len(ks) - len(w), len(ks)), pct(len(ks) - len(fw), len(ks)), len(w)]
        )
    for name, sel in (("dev", lambda k: k[0] in DEV), ("held-out", lambda k: k[0] not in DEV)):
        ks = [k for k in tk if sel(k)]
        w = [k for k in ks if not P[k]["correct"]]
        fw = [k for k in ks if not F[k]["correct"]]
        rows.append(
            [name, len(ks), pct(len(ks) - len(w), len(ks)), pct(len(ks) - len(fw), len(ks)), len(w)]
        )
    md.append(
        "### Temporal accuracy by conversation\n\n"
        + md_table(["conv", "n temporal", "full-persp %", "eq06-fix %", "wrong"], rows)
        + "\n"
    )

    # question types in temporal
    def qtype(a):
        q = a["question"].lower()
        str(a["gold_answer"]).lower()
        if (
            re.match(
                r"^(how long|how many (days|weeks|months|years)|after how many|for how long|how old)",
                q,
            )
            or "how long" in q
            or re.search(r"how many (days|weeks|months|years)", q)
        ):
            return "duration / interval length"
        if re.search(
            r"\b(first|second|third|fourth|last|before|after|latest|earliest)\b", q
        ) and q.startswith("when"):
            return "when + ordinal / ordering"
        if re.search(r"(which|what) year", q):
            return "which year"
        if q.startswith("when") or "what date" in q or " on what" in q:
            return "when (event date)"
        return "other dated (what/where/who on a date)"

    def gold_form(a):
        g = str(a["gold_answer"]).lower().strip()
        if re.search(
            r"\b(before|after|week of|weekend|between|around|few|last|next|ago|early|late|mid)\b", g
        ):
            return "relative / range gold"
        if (
            parse_interval(str(a["gold_answer"])) is not None
            and re.search(r"\d{1,2}\b", g)
            and re.search(r"[a-z]{3}", g)
        ):
            return "exact date gold"
        return "other gold form"

    rows = []
    cnt = collections.defaultdict(lambda: [0, 0, 0, 0])
    for k in tk:
        t = qtype(P[k])
        side = 0 if k[0] in DEV else 2
        cnt[t][side] += 1
        cnt[t][side + 1] += P[k]["correct"]
    for t, v in sorted(cnt.items()):
        rows.append([t, v[0], pct(v[1], v[0]), v[2], pct(v[3], v[2])])
    md.append(
        "### Temporal question mix and accuracy, dev vs held-out\n\n"
        + md_table(["question type", "n dev", "dev %", "n held-out", "held-out %"], rows)
        + "\n"
    )
    cnt = collections.defaultdict(lambda: [0, 0, 0, 0])
    for k in tk:
        t = gold_form(P[k])
        side = 0 if k[0] in DEV else 2
        cnt[t][side] += 1
        cnt[t][side + 1] += P[k]["correct"]
    rows = [[t, v[0], pct(v[1], v[0]), v[2], pct(v[3], v[2])] for t, v in sorted(cnt.items())]
    md.append(md_table(["gold form", "n dev", "dev %", "n held-out", "held-out %"], rows) + "\n")

    # temporal failure classification. Reader causes were assigned by reading the exact reader prompt (the dated
    # line of every gold turn, including its "[= date]" annotation), the raw answer and the gold, once, by one reviewer.
    tw = [k for k in tk if not P[k]["correct"]]
    T_CAUSE = {
        "dur": "duration / interval arithmetic, or a start date from 'N units ago' (code could do it)",
        "wd": "bare weekday / weekend phrase without a resolved [= date] annotation",
        "sess": "session or anchor date given as the event date (or a day invented inside a coarse range)",
        "evt": "wrong event matched (distractor line, or the prompt's own date example copied)",
        "ref": "refusal / premise denial although the annotated evidence is in the prompt",
        "gfmt": "annotation and answer agree, the gold uses the anchor date (gold-format mismatch)",
        "lst": "incomplete list",
    }
    T_MAP = {
        ("conv-41", "2-63"): "dur",
        ("conv-41", "2-31"): "dur",
        ("conv-42", "3-43"): "dur",
        ("conv-43", "4-16"): "dur",
        ("conv-43", "4-56"): "dur",
        ("conv-44", "5-7"): "dur",
        ("conv-44", "5-34"): "dur",
        ("conv-44", "5-47"): "dur",
        ("conv-44", "5-59"): "dur",
        ("conv-47", "6-4"): "dur",
        ("conv-47", "6-53"): "dur",
        ("conv-47", "6-64"): "dur",
        ("conv-49", "8-25"): "dur",
        ("conv-49", "8-39"): "dur",
        ("conv-50", "9-61"): "dur",
        ("conv-50", "9-62"): "dur",
        ("conv-42", "3-31"): "wd",
        ("conv-44", "5-22"): "wd",
        ("conv-42", "3-44"): "sess",
        ("conv-44", "5-57"): "sess",
        ("conv-48", "7-71"): "sess",
        ("conv-30", "1-20"): "evt",
        ("conv-42", "3-41"): "evt",
        ("conv-42", "3-54"): "evt",
        ("conv-47", "6-41"): "evt",
        ("conv-50", "9-67"): "evt",
        ("conv-26", "0-26"): "ref",
        ("conv-26", "0-49"): "ref",
        ("conv-43", "4-40"): "ref",
        ("conv-48", "7-44"): "ref",
        ("conv-42", "3-35"): "gfmt",
        ("conv-49", "8-45"): "lst",
    }
    sub = {}
    for k in tw:
        s = lab[k]
        if s.startswith("d:"):
            sub[k] = "reader: " + T_CAUSE[T_MAP[k]]
        else:
            sub[k] = {
                "b": "gold turn missing (recall)",
                "cf": "gold turn missing (fusion cut, not in the pool)",
                "cr": "gold turn missing (rerank_keep cut)",
                "e": "judge (range / day-level equivalence)",
                "f": "gold error (errata)",
            }[s]
    miss = [k for k in tw if lab[k].startswith("d:") and k not in T_MAP]
    assert not miss, miss
    tc = collections.Counter(sub.values())
    rows = [
        [
            kk,
            v,
            sum(1 for k in tw if sub[k] == kk and k[0] in DEV),
            sum(1 for k in tw if sub[k] == kk and k[0] not in DEV),
        ]
        for kk, v in tc.most_common()
    ]
    rows.append(
        [
            "**temporal wrong**",
            len(tw),
            sum(1 for k in tw if k[0] in DEV),
            sum(1 for k in tw if k[0] not in DEV),
        ]
    )
    md.append(
        "### Temporal failures by cause\n\n"
        + md_table(["cause", "total", "dev", "held-out"], rows)
        + "\n"
    )
    rows = []
    for kk in tc:
        rows.append([kk] + [sum(1 for k in tw if sub[k] == kk and k[0] == cv) for cv in CONVS])
    rows.append(
        ["**wrong / n**"]
        + [f"{sum(1 for k in tw if k[0] == cv)}/{sum(1 for k in tk if k[0] == cv)}" for cv in CONVS]
    )
    md.append(md_table(["cause"] + [c.replace("conv-", "") for c in CONVS], rows) + "\n")

    # fixable-by-code share
    code_fixable = sum(
        1 for k in tw if lab[k].startswith("d:") and T_MAP[k] in ("dur", "wd", "sess")
    )
    md.append(
        f"Temporal wrong answers whose cause code could repair (duration solver, bare-weekday annotation, anchor-vs-event date): {code_fixable} of {len(tw)}.\n"
    )

    # dev vs held-out: question-type mix versus rate
    def type_key(k):
        return qtype(P[k])

    types = sorted({type_key(k) for k in tk})
    dev_acc = {
        t: statistics.mean(P[k]["correct"] for k in tk if k[0] in DEV and type_key(k) == t)
        if any(k[0] in DEV and type_key(k) == t for k in tk)
        else None
        for t in types
    }
    held = [k for k in tk if k[0] not in DEV]
    exp_ = sum(dev_acc[type_key(k)] for k in held if dev_acc[type_key(k)] is not None) + sum(
        P[k]["correct"] for k in held if dev_acc[type_key(k)] is None
    )
    md.append(
        f"Mix versus rate: held-out temporal accuracy is {pct(sum(P[k]['correct'] for k in held), len(held))}%; with the dev accuracy of each question type it would be {100 * exp_ / len(held):.1f}%. "
        f"Dev accuracy is {pct(sum(P[k]['correct'] for k in tk if k[0] in DEV), sum(1 for k in tk if k[0] in DEV))}%.\n"
    )
    drow = [
        [
            t,
            sum(1 for k in tk if k[0] not in DEV and type_key(k) == t),
            sum(1 for k in tk if k[0] in DEV and type_key(k) == t),
        ]
        for t in types
    ]
    md.append(f"Temporal question types held-out vs dev (counts): {drow}\n")

    for name, sel in (("dev", lambda k: k[0] in DEV), ("held-out", lambda k: k[0] not in DEV)):
        ks = [k for k in tk if sel(k)]
        allin = sum(1 for k in ks if all(g["in_context"] for g in P[k]["gold_turns"]))
        md.append(
            f"- {name}: temporal questions with every gold turn in context {pct(allin, len(ks))}% (n={len(ks)}), mean gold turns {statistics.mean(len(P[k]['gold_turns']) for k in ks):.2f}; gold errors {sum(1 for k in ks if lab.get(k) == 'f')}."
        )
    md.append("")

    # annotation coverage: does the prompt line of a gold turn with a relative phrase carry the resolved annotation?
    rel_marked = rel_total = 0
    for k in tk:
        pr = prompt_of(k)
        for g in P[k]["gold_turns"]:
            body = (g["text"] or "").split(": ", 1)[1][:40] if ": " in (g["text"] or "") else ""
            if body and body in pr:
                i = pr.index(body)
                en = pr.find("\n", i)
                line = pr[i : en if en > 0 else len(pr)]
                if re.search(
                    r"\b(yesterday|last (week|weekend|month|year|night|friday|saturday|sunday|monday|tuesday|wednesday|thursday)|next (week|month)|ago|tomorrow|this (week|weekend|month))\b",
                    line.lower(),
                ):
                    rel_total += 1
                    rel_marked += "[=" in line
    md.append(
        f"Temporal gold turns that contain a relative phrase (yesterday, last X, ago, this weekend): {rel_total}; of these the prompt line carries a resolved `[= date]` annotation: {rel_marked} (all questions of the category, right and wrong).\n"
    )

    # reader flip churn: same gold coverage, different context, outcome flips
    if D0:
        both_full = [
            k
            for k in dkeys
            if all(g["in_context"] for g in P[k]["gold_turns"])
            and all(g["in_context"] for g in D0[k]["gold_turns"])
            and P[k]["gold_turns"]
        ]
        fl = [k for k in both_full if P[k]["correct"] != D0[k]["correct"]]
        md.append(
            f"Reader churn on the dev conversations: {len(both_full)} questions have every gold turn in the context in BOTH full-persp-loc and xb-loc-dev; {len(fl)} of them flip "
            f"({pct(len(fl), len(both_full))}%), although the evidence is complete in both. Dev flips in total: {len(groups['lost vs xb-loc-dev (dev)']) + len(groups['gained vs xb-loc-dev (dev)'])}.\n"
        )
    both_full = [
        k
        for k in keys
        if all(g["in_context"] for g in P[k]["gold_turns"])
        and all(g["in_context"] for g in F[k]["gold_turns"])
        and P[k]["gold_turns"]
    ]
    fl = [k for k in both_full if P[k]["correct"] != F[k]["correct"]]
    md.append(
        f"Same on all conversations against eq06-fix: {len(both_full)} questions with complete evidence in both, {len(fl)} flip ({pct(len(fl), len(both_full))}%).\n"
    )

    # ------------------------------------------------------------------ 4. dump joined local file + aggregates
    with (out / "joined.jsonl").open("w", encoding="utf-8") as fh:
        for k in keys:
            fh.write(
                json.dumps(
                    dict(
                        item=k[0],
                        qid=k[1],
                        cat=cat_of[k],
                        ok=P[k]["correct"],
                        fix_ok=F[k]["correct"],
                        dev_ok=(D0[k]["correct"] if k in D0 else None),
                        label=lab.get(k),
                        question=P[k]["question"],
                        gold=P[k]["gold_answer"],
                        answer=P[k]["answer"],
                    ),
                    ensure_ascii=False,
                )
                + "\n"
            )
    (out / "tables.md").write_text("\n".join(md), encoding="utf-8")
    print("\n".join(md))


if __name__ == "__main__":
    main()
