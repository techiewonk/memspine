"""GLIFT gap analysis: would lifting derived hits to source turns rescue the graph run's losses?

Reads local forensics + results for a graph run (P), the J reference (C) and the dataset file.
Forensics carry no record -> parents mapping (the runs use a tempdir store, nothing persisted),
so the evidence of each derived hit is a PROXY, built from the record text:

* composite window (session / episode text with turn lines): the turns whose text it contains
  (exact);
* fact / dated fact / entity summary: the top-K turns of the question's conversation by idf-weighted
  word overlap with the fact (restricted to the session of its date when it carries one). This is
  the same signal the ``evidence+match`` lift uses (similarity of parents to the query), seen from
  the fact side, so it is an upper-ish bound for facts whose parents are session-wide.

Writes ``evals/analysis/glift_gaps.json`` (ids and counts only) and, with ``--text-out``, a
text-bearing per-question dump for the gitignored ``evals/runs/_analysis/``.

Usage: python evals/analysis/glift_gaps.py --runs <evals/runs> --data <locomo10.json> [--p v2-P2REF-full]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from pathlib import Path

DIA = re.compile(r"^D\d+:\d+$")
DIA_ALL = re.compile(r"D\d+:\d+")
ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
WALLCLOCK = re.compile(r"\b202[56]\b")
SPEAKER = re.compile(r"(?:^|\s)[A-Z][a-z]+: ")
WORD = re.compile(r"[a-z0-9']+")
STOP = set(
    [
        "the",
        "a",
        "an",
        "of",
        "to",
        "and",
        "in",
        "on",
        "at",
        "for",
        "with",
        "is",
        "was",
        "were",
        "be",
        "been",
        "he",
        "she",
        "they",
        "it",
        "his",
        "her",
        "their",
        "that",
        "this",
        "i",
        "you",
        "we",
        "my",
        "your",
    ]
)
K = 3  # derived_lift_max_parents default
MONTHS = {
    m: i
    for i, m in enumerate(
        [
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
        ],
        1,
    )
}


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf8").splitlines() if x.strip()]


def derived_kind(text: str) -> str:
    if text.startswith("ABOUT (entity summaries"):
        return "entity_summary"
    if len(SPEAKER.findall(text)) >= 2:
        return "composite_window"
    if re.match(r"^\[\d{4}-\d{2}-\d{2}", text):
        return "dated_fact"
    return "undated_fact"


def words(text: str) -> set[str]:
    return {w for w in WORD.findall(text.lower()) if w not in STOP and len(w) > 1}


def tokens(text: str) -> set[str]:
    return set(ISO.findall(text)) | set(YEAR.findall(text))


class Conv:
    def __init__(self, c: dict) -> None:
        conv = c["conversation"]
        self.turns: dict[str, str] = {}
        self.sess_of: dict[str, str] = {}
        self.sess_iso: dict[str, str] = {}
        for k, v in conv.items():
            if re.fullmatch(r"session_\d+", k):
                m = re.search(
                    r"(\d+):(\d+) (am|pm) on (\d+) (\w+), (\d{4})", conv.get(k + "_date_time", "")
                )
                if m and m.group(5).lower() in MONTHS:
                    self.sess_iso[k] = (
                        f"{m.group(6)}-{MONTHS[m.group(5).lower()]:02d}-{int(m.group(4)):02d}"
                    )
                for t in v:
                    self.turns[t["dia_id"]] = f"{t['speaker']}: {t['text']}"
                    self.sess_of[t["dia_id"]] = k
        self.w = {d: words(t) for d, t in self.turns.items()}
        df: Counter = Counter()
        for ws in self.w.values():
            df.update(ws)
        n = len(self.w)
        self.idf = {w: math.log(1 + n / c_) for w, c_ in df.items()}
        self.gold: dict[str, set[str]] = {}

    def match(self, text: str, k: int) -> list[str]:
        mine = words(text)
        dates = ISO.findall(text)
        pool = list(self.turns)
        if dates:
            sess = {s for s, d in self.sess_iso.items() if d in dates}
            if sess:
                pool = [t for t in pool if self.sess_of[t] in sess] or pool
        sc = sorted(
            ((sum(self.idf.get(w, 0) for w in mine & self.w[t]), t) for t in pool), reverse=True
        )
        return [t for s, t in sc[:k] if s > 0]

    def contained(self, text: str) -> list[str]:
        return [d for d, t in self.turns.items() if len(t) > 25 and t in text]


def proxy_evidence(cv: Conv, text: str, kind: str) -> tuple[list[str], str]:
    if kind == "composite_window":
        ex = cv.contained(text)
        if ex:
            return ex, "exact"
    return cv.match(text, K), "match_top3"


def analyse(runs: Path, p: str, c: str, convs: dict[str, Conv]) -> dict:
    P = {r["query_id"]: r for r in _load(runs / f"{p}--forensics/forensics.jsonl")}
    C = {r["query_id"]: r for r in _load(runs / f"{c}--forensics/forensics.jsonl")}
    Pr = {
        r["query_id"]: r
        for r in _load(runs / f"{p}--memspine/results.jsonl")
        if r.get("kind") == "result"
    }
    Cr = {
        r["query_id"]: r
        for r in _load(runs / f"{c}--memspine/results.jsonl")
        if r.get("kind") == "result"
    }
    qs = [q for q in Pr if q in Cr and q in P and q in C]
    out: dict = {"p": p, "c": c, "n_common": len(qs), "cats": {}, "text_rows": []}
    for cat in ("cat1", "cat2", "cat3", "cat4"):
        cq = [q for q in qs if Pr[q]["type_label"] == cat]
        broken = [q for q in cq if float(Cr[q]["score"]) == 1 and float(Pr[q]["score"]) == 0]
        fixed = [q for q in cq if float(Cr[q]["score"]) == 0 and float(Pr[q]["score"]) == 1]
        gold_ok = [
            q
            for q in cq
            if (g := convs[Pr[q]["item_id"]].gold.get(q))
            and g <= {r["turn"] for r in C[q]["context_records"]}
        ]
        rate = sum(float(Cr[q]["score"]) for q in gold_ok) / max(1, len(gold_ok))
        res: dict = {
            "n": len(cq),
            "broken_n": len(broken),
            "fixed_n": len(fixed),
            "broken": {},
            "fixed": {},
        }
        for tag, ids in (("broken", broken), ("fixed", fixed)):
            cause: dict[str, list[str]] = {}
            slots: Counter = Counter()
            kinds: Counter = Counter()
            chans: Counter = Counter()
            rank_hist: Counter = Counter()
            n_with_useful = 0
            for q in ids:
                cv = convs[Pr[q]["item_id"]]
                gold = cv.gold.get(q, set())
                pf, cf = P[q], C[q]
                ctx = {r["turn"] for r in pf["context_records"] if DIA.match(r["turn"])}
                cctx = {r["turn"] for r in cf["context_records"] if DIA.match(r["turn"])}
                text_of = {r["turn"]: r["text"] for r in pf["context_records"]}
                legs = {
                    "vector": {h["turn"] for h in pf["vector"]},
                    "lexical": {h["turn"] for h in pf["lexical"]},
                    "extra": {h["turn"] for h in pf["extra_legs"].get("extra", [])},
                    "perspective": {h["turn"] for h in pf["extra_legs"].get("perspective", [])},
                }
                dhits = []
                for h in pf["final"]:
                    if DIA.match(h["turn"]):
                        continue
                    txt = text_of.get(h["turn"], "")
                    kind = derived_kind(txt) if txt else "unknown"
                    ev, how = proxy_evidence(cv, txt, kind) if txt else ([], "none")
                    chan = [n for n, s in legs.items() if h["turn"] in s]
                    dhits.append(
                        {
                            "rank": h["r"],
                            "kind": kind,
                            "channel": chan or ["fused_only"],
                            "proxy": how,
                            "n_ev": len(ev),
                            "gold_in_ev": sorted(gold & set(ev)),
                            "ev": ev,
                        }
                    )
                    kinds[kind] += 1
                    for ch in chan or ["fused_only"]:
                        chans[ch] += 1
                    rank_hist[h["r"]] += 1
                n_der = len(dhits)
                slots[n_der] += 1
                missing = (gold & cctx) - ctx  # gold turns the J reference had and this run lost
                lift_ev = {g for d in dhits for g in d["gold_in_ev"]}
                turn_rank = [x["turn"] for x in pf["rerank_scores"] if DIA.match(x["turn"])]
                pool_ids = {x["turn"] for x in pf["pool"]}
                ans = Pr[q]["answer"]
                dtext = " ".join(
                    text_of.get(h["turn"], "") for h in pf["final"] if not DIA.match(h["turn"])
                )
                tturn = " ".join(r["text"] for r in pf["context_records"] if DIA.match(r["turn"]))
                foreign = (tokens(ans) & tokens(dtext)) - tokens(tturn)
                if not (gold & cctx) and tag == "broken":
                    k = "c0_gold_absent_in_clean"
                elif tag == "fixed":
                    k = (
                        "fixed_gold_in_graph_ctx"
                        if gold & ctx
                        else "fixed_gold_absent_in_graph_ctx"
                    )
                    if gold & ctx and dhits and not any(d["gold_in_ev"] for d in dhits):
                        k += "_derived_not_needed"
                elif not missing:
                    if foreign or (WALLCLOCK.search(ans) and WALLCLOCK.search(dtext)):
                        k = "iv_date_error"
                    elif n_der >= 4:
                        k = "gold_present_derived_heavy"
                    else:
                        k = "gold_present_other"
                elif missing & lift_ev:
                    k = "i_evidence_has_gold"
                elif all(g in turn_rank[:10] for g in missing):
                    k = "iii_displacement_only"
                elif n_der == 0:
                    k = "no_derived_gold_missing"
                elif any(g in pool_ids for g in missing):
                    k = "ii_no_useful_lineage_gold_in_pool"
                else:
                    k = "ii_no_useful_lineage_gold_not_in_pool"
                cause.setdefault(k, []).append(q)
                if any(d["gold_in_ev"] for d in dhits):
                    n_with_useful += 1
                out["text_rows"].append(
                    {
                        "qid": q,
                        "cat": cat,
                        "tag": tag,
                        "cause": k,
                        "n_derived_final": n_der,
                        "derived": dhits,
                        "missing_gold": sorted(missing),
                        "gold": Pr[q]["gold"],
                        "p_answer": ans,
                        "c_answer": Cr[q]["answer"],
                    }
                )
            res[tag] = {
                "cause_counts": {k: len(v) for k, v in sorted(cause.items())},
                "cause_ids": {k: v for k, v in sorted(cause.items())},
                "derived_slots_in_final_hist": dict(sorted(slots.items())),
                "mean_derived_slots": round(
                    sum(k * v for k, v in slots.items()) / max(1, len(ids)), 2
                ),
                "derived_kind_counts": dict(kinds),
                "derived_channel_counts": dict(chans),
                "derived_rank_hist": dict(sorted(rank_hist.items())),
                "questions_with_gold_in_some_derived_evidence": n_with_useful,
            }
        b = res["broken"]["cause_counts"]
        rescue_ids = (
            b.get("i_evidence_has_gold", 0)
            + b.get("iii_displacement_only", 0)
            + b.get("iv_date_error", 0)
        )
        res["clean_acc_given_gold_in_ctx"] = round(rate, 3)
        res["lift_rescue_ceiling"] = rescue_ids
        res["lift_rescue_expected"] = round(rescue_ids * rate, 1)
        res["lift_rescue_expected_i_only"] = round(b.get("i_evidence_has_gold", 0) * rate, 1)
        out["cats"][cat] = res
    for tag in ("broken", "fixed"):
        agg: Counter = Counter()
        for r in out["cats"].values():
            agg.update(r[tag]["cause_counts"])
        out[f"{tag}_total"] = dict(sorted(agg.items()))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--p", action="append", default=None)
    ap.add_argument("--c", default="clean-jina-full")
    ap.add_argument("--out", default=str(Path(__file__).with_name("glift_gaps.json")))
    ap.add_argument("--text-out", default=None)
    a = ap.parse_args()
    data = json.loads(Path(a.data).read_text(encoding="utf8"))
    convs: dict[str, Conv] = {}
    for ci, c in enumerate(data):
        cv = Conv(c)
        cv.gold = {
            f"{ci}-{qi}": set(DIA_ALL.findall(" ".join(qa.get("evidence", []))))
            for qi, qa in enumerate(c["qa"])
        }
        convs[c["sample_id"]] = cv
    result: dict = {
        "note": "evidence is a text proxy: forensics carry no record->parents mapping",
        "k": K,
        "runs": {},
    }
    text_rows: list[dict] = []
    for p in a.p or ["v2-P2REF-full"]:
        r = analyse(Path(a.runs), p, a.c, convs)
        text_rows += [{**x, "run": p} for x in r.pop("text_rows")]
        result["runs"][p] = r
        print(p, r["n_common"], "broken", r["broken_total"], "fixed", r["fixed_total"])
        for cat, v in r["cats"].items():
            print(" ", cat, v["broken_n"], v["lift_rescue_ceiling"], v["lift_rescue_expected"])
    Path(a.out).write_text(json.dumps(result, indent=1), encoding="utf8")
    if a.text_out:
        Path(a.text_out).write_text("\n".join(json.dumps(x) for x in text_rows), encoding="utf8")


if __name__ == "__main__":
    main()
