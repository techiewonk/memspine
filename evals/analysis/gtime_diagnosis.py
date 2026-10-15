"""GTIME diagnosis: why does a graph run with ``read.derived_lift`` still lose time questions?

Reads local forensics + results of a graph run (P, default ``v2-G2F-full``), the J reference
(C, ``clean-jina-full``) and the dataset file. Writes ``evals/analysis/gtime_diagnosis.json``
(ids and counts only). With ``--text-out`` it writes a text-bearing per-question dump for the
gitignored ``evals/runs/_analysis/`` (no text is ever committed).

Per question it compares the legs, the pool, the rerank and the final context of P and C:

* cause of each cat2 loss (gold turn missing from the context, or present with a wrong date);
* where the date the reader answered came from (a derived "About" block versus the turns);
* which leg supplied the turns that took the places of C's final turns (graph legs, the lift,
  or the shared vector and lexical legs reordered);
* an offline simulation of the guard: drop the graph-leg-only and lifted candidates of a
  time-shaped question from P's pool, re-cut the final top 10 by P's own rerank scores, and see
  whether the gold turns enter (the refill from the base legs is not modelled, so this is a
  floor for what a real guarded read recovers).

Usage: python evals/analysis/gtime_diagnosis.py --runs <evals/runs> --data <locomo10.json>
(needs the memspine package on PYTHONPATH for ``is_time_shaped``).
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from memspine.models.encoders.query.shape import is_temporal, is_time_shaped

DIA = re.compile(r"^D\d+:\d+$")
DIA_ALL = re.compile(r"D\d+:\d+")
ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]
MON = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
SUMMARY = "ABOUT (entity summaries"
CATS = ("cat1", "cat2", "cat3", "cat4")


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf8").splitlines() if x.strip()]


def _mnum(s: str) -> int:
    return next(i for i, m in enumerate(MONTHS, 1) if m.startswith(s[:3].lower()))


def year_months(text: str) -> set[tuple[int, int]]:
    """Every (year, month) a text names: ISO dates, '3 May 2023', 'May 3, 2023', 'May 2023'."""
    out: set[tuple[int, int]] = set()
    for y, m, _d in re.findall(r"\b(\d{4})-(\d{2})-(\d{2})\b", text):
        out.add((int(y), int(m)))
    for _d, m, y in re.findall(r"\b(\d{1,2})\s+" + MON + r"\w*\.?,?\s+(\d{4})\b", text, re.I):
        out.add((int(y), _mnum(m)))
    for m, _d, y in re.findall(
        r"\b" + MON + r"\w*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})\b", text, re.I
    ):
        out.add((int(y), _mnum(m)))
    for m, y in re.findall(r"\b" + MON + r"\w*\s+(\d{4})\b", text, re.I):
        out.add((int(y), _mnum(m)))
    return out


def gold_map(data: list[dict]) -> tuple[dict[str, str], dict[str, set[str]]]:
    item_of: dict[str, str] = {}
    gold: dict[str, set[str]] = {}
    for ci, c in enumerate(data):
        for qi, qa in enumerate(c["qa"]):
            qid = f"{ci}-{qi}"
            item_of[qid] = c["sample_id"]
            gold[qid] = set(DIA_ALL.findall(" ".join(qa.get("evidence", []))))
    return item_of, gold


def origins(f: dict) -> dict[str, str]:
    """For each candidate in P's pool: where it came from.

    ``graph`` = only in the graph legs (``extra`` is the graph leg, ``graph_nodes`` the node search);
    ``lift`` = a turn in no leg list (it arrived by replacing a derived hit); ``base`` = in a base leg.
    """
    base = {h["turn"] for k in ("vector", "lexical") for h in f[k]} | {
        h["turn"] for h in f["extra_legs"].get("perspective", [])
    }
    graph = {h["turn"] for k in ("extra", "graph_nodes") for h in f["extra_legs"].get(k, [])}
    anyleg = base | graph
    out: dict[str, str] = {}
    for h in f["pool"]:
        t = h["turn"]
        out[t] = "base" if t in base else "graph" if t in graph else "lift"
    return out


def simulate_final(f: dict, drop: set[str]) -> list[str]:
    """P's final top 10 turns after removing ``drop`` ids from the pool (rerank scores as logged)."""
    scored = [
        (h["score"], h["turn"])
        for h in f["rerank_scores"]
        if h["turn"] not in drop and DIA.match(h["turn"])
    ]
    scored.sort(key=lambda p: -p[0])
    return [t for _s, t in scored[:10]]


def summary_only_date(f: dict, answer: str) -> bool:
    """The answer names a (year, month) that the "About" block has and the turns do not."""
    stext = " ".join(r["text"] for r in f["context_records"] if r["text"].startswith(SUMMARY))
    ttext = " ".join(r["text"] for r in f["context_records"] if DIA.match(r["turn"]))
    return bool(year_months(answer) & (year_months(stext) - year_months(ttext)))


def rate(rows: list[int]) -> float:
    return round(sum(rows) / len(rows), 3) if rows else 0.0


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--p", default="v2-G2F-full")
    ap.add_argument("--c", default="clean-jina-full")
    ap.add_argument("--out", default=str(Path(__file__).with_name("gtime_diagnosis.json")))
    ap.add_argument("--text-out", default=None)
    a = ap.parse_args()
    runs = Path(a.runs)
    item_of, gold = gold_map(json.loads(Path(a.data).read_text(encoding="utf8")))
    P = {r["query_id"]: r for r in _load(runs / f"{a.p}--forensics/forensics.jsonl")}
    C = {r["query_id"]: r for r in _load(runs / f"{a.c}--forensics/forensics.jsonl")}
    Pr = {
        r["query_id"]: r
        for r in _load(runs / f"{a.p}--memspine/results.jsonl")
        if r.get("kind") == "result"
    }
    Cr = {
        r["query_id"]: r
        for r in _load(runs / f"{a.c}--memspine/results.jsonl")
        if r.get("kind") == "result"
    }
    qs = [q for q in Pr if q in Cr and q in P and q in C]
    cat_of = {q: Pr[q]["type_label"] for q in qs}
    ok = lambda r, q: float(r[q]["score"]) == 1
    out: dict = {"p": a.p, "c": a.c, "n_common": len(qs), "by_cat": {}, "cat2": {}}
    text_rows: list[dict] = []

    for cat in CATS:
        cq = [q for q in qs if cat_of[q] == cat]
        br = [q for q in cq if ok(Cr, q) and not ok(Pr, q)]
        fx = [q for q in cq if not ok(Cr, q) and ok(Pr, q)]
        shaped = [q for q in cq if is_time_shaped(P[q]["query"])]
        out["by_cat"][cat] = {
            "n": len(cq),
            "broken": len(br),
            "fixed": len(fx),
            "net": len(fx) - len(br),
            "acc_c": rate([int(ok(Cr, q)) for q in cq]),
            "acc_p": rate([int(ok(Pr, q)) for q in cq]),
            "n_is_temporal": sum(is_temporal(P[q]["query"]) for q in cq),
            "n_time_shaped": len(shaped),
            "time_shaped": {
                "broken": [q for q in br if q in shaped],
                "fixed": [q for q in fx if q in shaped],
                "acc_c": rate([int(ok(Cr, q)) for q in shaped]),
                "acc_p": rate([int(ok(Pr, q)) for q in shaped]),
            },
            "not_time_shaped": {
                "broken": sum(q not in shaped for q in br),
                "fixed": sum(q not in shaped for q in fx),
            },
        }

    # --- shape x category net against C, for P and for J-config runs (the run-to-run noise) ---
    def shape(q: str) -> str:
        qq = P[q]["query"]
        return "when" if is_temporal(qq) else "other_time" if is_time_shaped(qq) else "none"

    def net_table(res: dict[str, dict]) -> dict:
        t: dict = {}
        for q in qs:
            if q not in res:
                continue
            d = int(float(res[q]["score"]) == 1) - int(ok(Cr, q))
            row = t.setdefault(cat_of[q], {}).setdefault(shape(q), {"n": 0, "plus": 0, "minus": 0})
            row["n"] += 1
            row["plus"] += d > 0
            row["minus"] += d < 0
        return t

    out["net_by_cat_and_shape_vs_c"] = {a.p: net_table(Pr)}
    out["noise_reference"] = {
        "note": "J-config full runs (no graph, no summaries) against the same C: run-to-run and flag-to-flag variation"
    }
    for ref in ("v2-P3J-full", "r7-protect-full", "r7-post-full"):
        path = runs / f"{ref}--memspine/results.jsonl"
        if path.exists():
            out["noise_reference"][ref] = net_table(
                {r["query_id"]: r for r in _load(path) if r.get("kind") == "result"}
            )

    # --- cat2: cause of each loss ---------------------------------------------------------
    cat2 = [q for q in qs if cat_of[q] == "cat2"]
    broken = [q for q in cat2 if ok(Cr, q) and not ok(Pr, q)]
    fixed = [q for q in cat2 if not ok(Cr, q) and ok(Pr, q)]
    summary_in_p = Counter(
        any(r["text"].startswith(SUMMARY) for r in P[q]["context_records"]) for q in cat2
    )
    summary_in_c = Counter(
        any(r["text"].startswith(SUMMARY) for r in C[q]["context_records"]) for q in cat2
    )
    causes: dict[str, list[str]] = {}
    origin_cnt: Counter = Counter()
    foreign_sess: Counter = Counter()
    for q in broken:
        pf, cf = P[q], C[q]
        g = gold[q]
        pctx = {r["turn"] for r in pf["context_records"]}
        cctx = {r["turn"] for r in cf["context_records"]}
        pool = origins(pf)
        stext = " ".join(r["text"] for r in pf["context_records"] if r["text"].startswith(SUMMARY))
        ttext = " ".join(r["text"] for r in pf["context_records"] if DIA.match(r["turn"]))
        ans, gd = year_months(Pr[q]["answer"]), year_months(Pr[q]["gold"])
        wrong_date = bool(ans) and bool(gd) and not (ans & gd)
        from_summary = bool(ans & year_months(stext) - year_months(ttext))
        if g & cctx and not g <= pctx:
            if g & set(pool):
                k = "gold_lost_in_pool_displaced"
            else:
                k = "gold_lost_not_in_pool"
        elif not (g & cctx):
            k = "c0_gold_absent_in_clean"
        elif from_summary and wrong_date:
            k = "gold_present_wrong_date_from_summary"
        elif from_summary:
            k = "gold_present_summary_date_not_wrong_month"
        elif wrong_date:
            k = "gold_present_wrong_date_from_turns"
        else:
            k = "gold_present_no_date_error_reader_or_judge"
        causes.setdefault(k, []).append(q)
        # who took the places C's final turns held (the turns P's final has and C's final lacks)
        pfin = [h["turn"] for h in pf["final"] if DIA.match(h["turn"])]
        cfin = {h["turn"] for h in cf["final"] if DIA.match(h["turn"])}
        for t in pfin:
            if t in cfin:
                continue
            origin_cnt[pool.get(t, "none")] += 1
            if t not in g:
                foreign_sess["displacing_turns"] += 1
        text_rows.append(
            {
                "qid": q,
                "cause": k,
                "query": pf["query"],
                "gold": Pr[q]["gold"],
                "p_answer": Pr[q]["answer"],
                "c_answer": Cr[q]["answer"],
                "gold_turns": sorted(g),
                "gold_in_p_ctx": sorted(g & pctx),
                "summary_dates": sorted(f"{y}-{m:02d}" for y, m in year_months(stext)),
            }
        )
    out["cat2"] = {
        "n": len(cat2),
        "broken": len(broken),
        "fixed": len(fixed),
        "net": len(fixed) - len(broken),
        "summary_block_in_p_context": {str(k): v for k, v in summary_in_p.items()},
        "summary_block_in_c_context": {str(k): v for k, v in summary_in_c.items()},
        "derived_in_final": sum(any(not DIA.match(h["turn"]) for h in P[q]["final"]) for q in cat2),
        "cause_counts": {k: len(v) for k, v in sorted(causes.items())},
        "cause_ids": {k: v for k, v in sorted(causes.items())},
        "turns_in_p_final_not_in_c_final_by_origin_broken": dict(origin_cnt),
        "fixed_ids": fixed,
        "broken_ids": broken,
    }

    # --- cat2: is the loss explained by the explicit date or the relative date? -------------
    out["cat2"]["broken_question_shape"] = {
        "is_temporal": sum(is_temporal(P[q]["query"]) for q in broken),
        "time_shaped_only": sum(
            is_time_shaped(P[q]["query"]) and not is_temporal(P[q]["query"]) for q in broken
        ),
        "neither": sum(not is_time_shaped(P[q]["query"]) for q in broken),
    }

    # --- simulation: drop graph-only + lifted candidates, re-cut the final ------------------
    sim: dict = {
        "note": "pool candidates supplied only by the graph legs or by the lift are dropped; final = top 10 turns of P's own rerank scores; the base-leg refill is not modelled (a floor)"
    }
    for cat in CATS:
        cq = [q for q in qs if cat_of[q] == cat and is_time_shaped(P[q]["query"])]
        gold_in = lambda fin, q: bool(gold[q]) and gold[q] <= set(fin)
        any_in = lambda fin, q: bool(gold[q] & set(fin))
        rows = {
            "n_time_shaped": len(cq),
            "gold_all_in_final": {"c": 0, "p": 0, "sim": 0},
            "gold_any_in_final": {"c": 0, "p": 0, "sim": 0},
            "gained_by_sim": [],
            "lost_by_sim": [],
            "overlap_with_c_final": {"p": 0.0, "sim": 0.0},
        }
        ov_p = ov_s = 0.0
        for q in cq:
            og = origins(P[q])
            drop = {t for t, o in og.items() if o in ("graph", "lift")}
            pfin = [h["turn"] for h in P[q]["final"] if DIA.match(h["turn"])]
            cfin = [h["turn"] for h in C[q]["final"] if DIA.match(h["turn"])]
            sfin = simulate_final(P[q], drop)
            for key, fin in (("c", cfin), ("p", pfin), ("sim", sfin)):
                rows["gold_all_in_final"][key] += gold_in(fin, q)
                rows["gold_any_in_final"][key] += any_in(fin, q)
            if gold_in(sfin, q) and not gold_in(pfin, q):
                rows["gained_by_sim"].append(q)
            if gold_in(pfin, q) and not gold_in(sfin, q):
                rows["lost_by_sim"].append(q)
            cs = set(cfin)
            ov_p += len(cs & set(pfin)) / max(1, len(cs | set(pfin)))
            ov_s += len(cs & set(sfin)) / max(1, len(cs | set(sfin)))
        rows["overlap_with_c_final"] = {
            "p": round(ov_p / max(1, len(cq)), 3),
            "sim": round(ov_s / max(1, len(cq)), 3),
        }
        rows["fixed_gold_only_via_graph_or_lift"] = [
            q
            for q in cq
            if not ok(Cr, q)
            and ok(Pr, q)
            and not gold_in([h["turn"] for h in C[q]["final"]], q)
            and gold_in([h["turn"] for h in P[q]["final"] if DIA.match(h["turn"])], q)
            and not gold_in(
                simulate_final(
                    P[q], {t for t, o in origins(P[q]).items() if o in ("graph", "lift")}
                ),
                q,
            )
        ]
        rows["fixed_answer_date_only_in_summary"] = [
            q
            for q in cq
            if not ok(Cr, q) and ok(Pr, q) and summary_only_date(P[q], Pr[q]["answer"])
        ]
        sim[cat] = rows
    out["simulation"] = sim

    # --- expected recovery (cat2) --------------------------------------------------------
    present = [
        q for q in cat2 if gold[q] and gold[q] <= {r["turn"] for r in C[q]["context_records"]}
    ]
    r_given = rate([int(ok(Cr, q)) for q in present])
    cc = out["cat2"]["cause_counts"]
    summ_attr = cc.get("gold_present_wrong_date_from_summary", 0) + cc.get(
        "gold_present_summary_date_not_wrong_month", 0
    )
    gold_present_all = sum(v for k, v in cc.items() if k.startswith("gold_present"))
    displaced = cc.get("gold_lost_in_pool_displaced", 0)
    out["expected_recovery_cat2"] = {
        "j_acc_given_gold_in_ctx": r_given,
        "blocks_only": {
            "lower_summary_attributed": summ_attr,
            "upper_all_gold_present": gold_present_all,
            "expected_lower": round(summ_attr * r_given, 1),
            "expected_upper": round(gold_present_all * r_given, 1),
        },
        "blocks_plus_legs_extra": {
            "gold_lost_in_pool_displaced": displaced,
            "expected": round(displaced * r_given, 1),
        },
    }
    Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf8")
    if a.text_out:
        Path(a.text_out).write_text("\n".join(json.dumps(x) for x in text_rows), encoding="utf8")
    print(json.dumps({k: out[k] for k in ("by_cat",)}, indent=1)[:1800])
    print(json.dumps(out["cat2"]["cause_counts"], indent=1))
    print(
        out["cat2"]["turns_in_p_final_not_in_c_final_by_origin_broken"],
        out["cat2"]["broken_question_shape"],
    )
    print(
        json.dumps(
            {
                c: {k: v for k, v in sim[c].items() if not isinstance(v, list)}
                | {"gained": len(sim[c]["gained_by_sim"]), "lost": len(sim[c]["lost_by_sim"])}
                for c in CATS
            },
            indent=1,
        )
    )
    print(out["expected_recovery_cat2"])
    print(json.dumps(out["net_by_cat_and_shape_vs_c"]))
    print(
        {c: len(sim[c]["fixed_answer_date_only_in_summary"]) for c in CATS},
        {c: len(sim[c]["fixed_gold_only_via_graph_or_lift"]) for c in CATS},
    )


if __name__ == "__main__":
    main()
