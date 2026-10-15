"""GTEMP diagnosis: why the GC graph base (P2REF) lost cat1/cat2 questions against the J reference.

Reads two local forensics runs (plus their results.jsonl) and the local dataset file. Writes
``evals/analysis/gtemp_diagnosis.json`` (ids and counts only) and, with ``--text-out``, a
text-bearing per-question dump meant for the gitignored ``evals/runs/_analysis/`` folder.

Usage: python evals/analysis/gtemp_diagnose.py --runs <evals/runs dir> --data <locomo10.json>
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

DIA = re.compile(r"^D\d+:\d+$")
ISO = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
YEAR = re.compile(r"\b(?:19|20)\d{2}\b")
# Dataset dates are 2022-2023; a run-date anchor (the build wall clock) is 2025+.
WALLCLOCK = re.compile(r"\b202[56]\b")
SPEAKER = re.compile(r"(?:^|\s)[A-Z][a-z]+: ")


def _load(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text(encoding="utf8").splitlines() if x.strip()]


def derived_kind(text: str) -> str:
    """Classify a non-turn context record by its rendered shape (no model, no ids)."""
    if text.startswith("ABOUT (entity summaries"):
        return "entity_summary"
    if len(SPEAKER.findall(text)) >= 2:
        return "composite_window"  # session / episode text with turn lines and inline facts
    if re.match(r"^\[\d{4}-\d{2}-\d{2}", text):
        return "dated_fact"
    return "undated_fact"


def tokens(text: str) -> set[str]:
    return {m.group(0) for m in ISO.finditer(text)} | {m.group(0) for m in YEAR.finditer(text)}


def classify(q: str, P: dict, C: dict, gold: set[str], ans: str) -> tuple[str, dict]:
    pc, cc = P[q]["context_records"], C[q]["context_records"]
    p_dia = {r["turn"] for r in pc if DIA.match(r["turn"])}
    c_dia = {r["turn"] for r in cc if DIA.match(r["turn"])}
    der = [r for r in pc if not DIA.match(r["turn"])]
    info = {
        "gold_in_clean": bool(gold) and gold <= c_dia,
        "gold_in_p2ref": bool(gold) and gold <= p_dia,
        "n_derived": len(der),
        "n_turns_p2ref": len(p_dia),
        "n_turns_clean": len(c_dia),
        "wallclock_derived": any(WALLCLOCK.search(r["text"]) for r in der),
    }
    info["gold_hits_p2ref"] = len(gold & p_dia)
    info["gold_hits_clean"] = len(gold & c_dia)
    info["derived_in_final"] = sum(1 for r in P[q]["final"] if not DIA.match(r["turn"]))
    if info["gold_hits_p2ref"] < info["gold_hits_clean"]:
        return "a_displaced", info
    turn_text = " ".join(r["text"] for r in pc if DIA.match(r["turn"]))
    dtext = " ".join(r["text"] for r in der)
    foreign = (tokens(ans) & tokens(dtext)) - tokens(turn_text)
    info["answer_date_from_derived"] = len(foreign)
    info["answer_wallclock"] = bool(WALLCLOCK.search(ans))
    if foreign or (info["answer_wallclock"] and info["wallclock_derived"]):
        return "b_derived_date_led", info
    if info["gold_in_p2ref"]:
        if info["derived_in_final"] >= 4:
            return "c1_gold_present_derived_heavy_final", info
        return "c2_gold_present_other", info
    return "c_other_gold_absent_both", info


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", required=True)
    ap.add_argument("--data", required=True)
    ap.add_argument("--p", default="v2-P2REF-full")
    ap.add_argument("--c", default="clean-jina-full")
    ap.add_argument("--out", default=str(Path(__file__).with_name("gtemp_diagnosis.json")))
    ap.add_argument("--text-out", default=None)
    a = ap.parse_args()
    runs = Path(a.runs)
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
    data = json.loads(Path(a.data).read_text(encoding="utf8"))
    gold_of: dict[str, set[str]] = {}
    for ci, conv in enumerate(data):
        for qi, qa in enumerate(conv["qa"]):
            gold_of[f"{ci}-{qi}"] = {e for e in qa.get("evidence", []) if DIA.match(e)}
    out: dict = {"p": a.p, "c": a.c, "cats": {}}
    text_rows = []
    kinds_all: Counter = Counter()
    for r in P.values():
        for rec in r["context_records"]:
            if not DIA.match(rec["turn"]):
                kinds_all[derived_kind(rec["text"])] += 1
    out["derived_kinds_all_queries"] = dict(kinds_all)
    out["derived_total_records"] = sum(kinds_all.values())
    out["derived_wallclock_records"] = sum(
        1
        for r in P.values()
        for x in r["context_records"]
        if not DIA.match(x["turn"]) and WALLCLOCK.search(x["text"])
    )
    out["clean_nonturn_records"] = sum(
        1 for r in C.values() for x in r["context_records"] if not DIA.match(x["turn"])
    )
    for cat in ("cat1", "cat2", "cat3", "cat4"):
        qs = [q for q in Pr if Pr[q]["type_label"] == cat and q in Cr]
        broken = [q for q in qs if Cr[q]["score"] == 1 and Pr[q]["score"] == 0]
        fixed = [q for q in qs if Cr[q]["score"] == 0 and Pr[q]["score"] == 1]
        causes: dict[str, list[str]] = {}
        for q in broken:
            c, info = classify(q, P, C, gold_of.get(q, set()), Pr[q]["answer"])
            causes.setdefault(c, []).append(q)
            if a.text_out:
                kinds = Counter(
                    derived_kind(x["text"])
                    for x in P[q]["context_records"]
                    if not DIA.match(x["turn"])
                )
                text_rows.append(
                    {
                        "qid": q,
                        "cat": cat,
                        "cause": c,
                        **info,
                        "derived_kinds": dict(kinds),
                        "gold": Pr[q]["gold"],
                        "p_answer": Pr[q]["answer"],
                        "c_answer": Cr[q]["answer"],
                    }
                )
        wall_q = {
            q
            for q in qs
            if any(
                WALLCLOCK.search(x["text"])
                for x in P[q]["context_records"]
                if not DIA.match(x["turn"])
            )
        }
        # Offline estimate (no re-pack: the windows need the snapshot). The fix restores the turn
        # slots (raw_turn_floor) and the source-turn date (derived_dates), so a-type and b-type
        # questions return to the clean context; the clean arm answers a question correctly with
        # its gold in context at the measured rate below.
        gold_ok = [
            q
            for q in qs
            if gold_of.get(q) and gold_of[q] <= {r["turn"] for r in C[q]["context_records"]}
        ]
        rate = sum(Cr[q]["score"] for q in gold_ok) / max(1, len(gold_ok))
        n_ab = len(causes.get("a_displaced", [])) + len(causes.get("b_derived_date_led", []))
        n_c1 = len(causes.get("c1_gold_present_derived_heavy_final", []))
        estimate = {
            "ceiling_a_plus_b": n_ab,
            "clean_acc_given_gold_in_ctx": round(rate, 3),
            "expected_recovered_a_plus_b": round(n_ab * rate, 1),
            "possible_extra_c1": n_c1,
        }
        out["cats"][cat] = {
            "n": len(qs),
            "offline_estimate": estimate,
            "broken_ids": broken,
            "fixed_n": len(fixed),
            "cause_counts": {k: len(v) for k, v in sorted(causes.items())},
            "cause_ids": {k: v for k, v in sorted(causes.items())},
            "wallclock_derived_in_ctx": {
                "queries": len(wall_q),
                "of_broken": len([q for q in broken if q in wall_q]),
                "p2ref_acc_with": round(
                    sum(Pr[q]["score"] for q in wall_q) / max(1, len(wall_q)), 4
                ),
                "p2ref_acc_without": round(
                    sum(Pr[q]["score"] for q in qs if q not in wall_q)
                    / max(1, len(qs) - len(wall_q)),
                    4,
                ),
            },
        }
    Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf8")
    if a.text_out:
        Path(a.text_out).write_text("\n".join(json.dumps(x) for x in text_rows), encoding="utf8")
    for cat, v in out["cats"].items():
        print(cat, v["n"], "broken", len(v["broken_ids"]), v["cause_counts"])
    print(
        out["derived_kinds_all_queries"],
        out["derived_wallclock_records"],
        out["derived_total_records"],
        out["clean_nonturn_records"],
    )


if __name__ == "__main__":
    main()
