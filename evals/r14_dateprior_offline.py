"""R14-M-3 / R14-M-7 offline CPU check: which gold turns would enter the rerank pool.

Reads a reference run's forensics (per-leg ranks to depth 60) plus the local dataset's session
dates, re-fuses with RRF, and counts gold turns in the pool under ``pool_protect_per_leg`` = 0/3/8
and under
``read.date_prior``. Writes ids and counts only (no dataset text).

Usage: python evals/r14_dateprior_offline.py RUNS_DIR RUN_ID DATASET_JSON R14_MASTER_JSON OUT_JSON
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from memspine.read.pipeline.date_prior import (
    PROTECT_LEG_DEPTH,
    PROTECT_MAX,
    apply_prior,
    prior_span,
)

RRF_K = 60
POOL = 20
WINDOW, BOOST = 14, float(os.environ.get("DATE_PRIOR_BOOST", "1.0"))


def _parse(text: str) -> datetime:
    m = re.search(r"(\d{1,2}) (\w+),? (\d{4})", text)
    return datetime.strptime(f"{m[1]} {m[2]} {m[3]}", "%d %B %Y").replace(tzinfo=UTC)


def session_dates(path: Path) -> dict[str, dict[int, datetime]]:
    out: dict[str, dict[int, datetime]] = {}
    for conv in json.loads(path.read_text(encoding="utf-8")):
        dates: dict[int, datetime] = {}
        for k, v in conv["conversation"].items():
            m = re.fullmatch(r"session_(\d+)_date_time", k)
            if m:
                dates[int(m[1])] = _parse(v)
        out[conv["sample_id"]] = dates
    return out


def sess_of(turn: str) -> int:
    return int(turn.split(":")[0][1:])


def legs_of(f: dict) -> dict[str, list[str]]:
    legs = {
        "vector": [h["turn"] for h in f["vector"]],
        "lexical": [h["turn"] for h in f["lexical"]],
    }
    for name, hits in (f.get("extra_legs") or {}).items():
        legs[name] = [h["turn"] for h in hits]
    return legs


def rrf(legs: dict[str, list[str]]) -> list[tuple[str, float]]:
    score: dict[str, float] = {}
    for hits in legs.values():
        for r, t in enumerate(hits, start=1):
            score[t] = score.get(t, 0.0) + 1.0 / (RRF_K + r)
    return sorted(score.items(), key=lambda p: p[1], reverse=True)


class _Rec:
    """The two fields ``apply_prior`` reads from a record."""

    def __init__(self, when: datetime | None) -> None:
        self.valid_from = when or datetime(1900, 1, 1, tzinfo=UTC)
        self.tags: list[str] = []


def main() -> None:
    runs, run_id, dataset, master, out = sys.argv[1:6]
    dates = session_dates(Path(dataset))
    forensics: dict[str, dict] = {}
    with open(Path(runs) / f"{run_id}--forensics" / "forensics.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            forensics[d["query_id"]] = d
    gold: dict[str, set[str]] = {}
    pq = Path(runs) / "_analysis" / f"fx_{run_id}" / "per_question.jsonl"
    with open(pq, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            gold[d["qid"]] = {g["turn"] for g in (d.get("gold_turns") or [])}
    items = {i["id"]: i for i in json.loads(Path(master).read_text(encoding="utf-8"))["items"]}
    ids = {k: items[k]["evidence"]["question_ids"] for k in ("R14-M-3", "R14-M-7")}

    def pool_for(qid: str, n: int, prior: bool) -> set[str]:
        f = forensics[qid]
        legs = legs_of(f)
        fused = rrf(legs)
        prot = {t for hits in legs.values() for t in hits[:n]} if n else set()
        if prior:
            span = prior_span(f["query"])
            if span:
                recs = {t: _Rec(dates[f["item"]].get(sess_of(t))) for t, _ in fused}
                fused, boosted = apply_prior(fused, recs, span, window_days=WINDOW, boost=BOOST)
                best: dict[str, int] = {}
                for hits in legs.values():
                    for r, t in enumerate(hits, start=1):
                        best[t] = min(best.get(t, 10**9), r)
                elig = sorted(
                    (t for t in boosted if best.get(t, 10**9) <= PROTECT_LEG_DEPTH),
                    key=lambda t: (-boosted[t], best[t]),
                )[:PROTECT_MAX]
                prot |= set(elig)
        return {t for t, _ in fused[:POOL]} | prot

    result: dict = {"run": run_id, "pool": POOL, "window_days": WINDOW, "boost": BOOST}
    settings = {
        "protect0": (0, False),
        "protect3": (3, False),
        "protect8": (8, False),
        "date_prior": (0, True),
        "date_prior+protect3": (3, True),
    }
    for key, qids in ids.items():
        qids = [q for q in qids if q in forensics]
        rows: dict = {"n_questions": len(qids)}
        for name, (n, prior) in settings.items():
            q_any = q_all = g_in = g_tot = 0
            newly: list[str] = []
            for q in qids:
                g = gold.get(q, set())
                p = pool_for(q, n, prior)
                g_in += len(g & p)
                g_tot += len(g)
                q_any += bool(g & p)
                q_all += bool(g) and g <= p
                if (g & p) - pool_for(q, 0, False):
                    newly.append(q)
            rows[name] = {
                "questions_any_gold_in_pool": q_any,
                "questions_all_gold_in_pool": q_all,
                "gold_turns_in_pool": g_in,
                "gold_turns_total": g_tot,
                "questions_with_new_gold_vs_protect0": len(newly),
                "ids_new": newly,
            }
        result[key] = rows
    lost: list[str] = []
    spanned = 0
    for q, f in forensics.items():
        if prior_span(f["query"]) is None or not gold.get(q):
            continue
        spanned += 1
        if gold[q] <= pool_for(q, 3, False) and not gold[q] <= pool_for(q, 3, True):
            lost.append(q)
    result["guard_date_questions"] = {
        "with_short_span": spanned,
        "lost_gold_with_prior_vs_protect3": len(lost),
        "ids_lost": lost,
    }
    Path(out).write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(json.dumps(result, indent=1))


if __name__ == "__main__":
    main()
