"""Offline (CPU) fidelity test of light rerankers against the saved Qwen3-Reranker-4B scores.

    python screening_reranker_fidelity.py score  CANDIDATE [--n 150] [--run r7-protect-full]
    python screening_reranker_fidelity.py report [--run r7-protect-full]

`score` re-scores the SAME candidate pools the 4B saw (evals/runs/<run>--forensics/forensics.jsonl: query, pool turn
ids, 4B rerank_scores) with a light reranker on CPU, on a seeded category-stratified sample, and caches the scores in
a scratch dir. Document text = the engine's `concat_background` header + the stored turn text (ingest.jsonl), query
= the raw question, as the engine passes them (rerank_date_prefix and rerank_context off). `report` prints the table.
No GPU is touched: CUDA is hidden from every model.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import time
from collections import defaultdict
from pathlib import Path

os.environ["CUDA_VISIBLE_DEVICES"] = ""  # CPU only: the GPU belongs to the eval queue
HERE = Path(__file__).resolve().parent
EVALS = HERE.parent
ROOT = EVALS.parent
OUT = Path(os.environ.get("SCR_OUT", HERE / "_scr_cache"))
SEED = 20261011
HEADER = "[type: episodic | channel: messages]\n"

CANDS = {
    "qwen06": ("Qwen/Qwen3-Reranker-0.6B", "qwen"),
    "jina35": ("jinaai/jina-reranker-v3.5", "jina"),
    "bgem3": ("BAAI/bge-reranker-v2-m3", "ce"),
    "bgebase": ("BAAI/bge-reranker-base", "ce"),
    "minilm": ("cross-encoder/ms-marco-MiniLM-L-6-v2", "ce"),
}


def load(run: str):
    fx = {}
    for line in open(EVALS / "runs" / f"{run}--forensics" / "forensics.jsonl", encoding="utf8"):
        d = json.loads(line)
        fx[(d["item"], d["query_id"])] = d
    text = {}
    for line in open(EVALS / "runs" / f"{run}--forensics" / "ingest.jsonl", encoding="utf8"):
        d = json.loads(line)
        if d.get("written"):
            text[(d["item"], d["turn"])] = d["stored_text"]
    gold = {}
    for ci, conv in enumerate(json.load(open(ROOT / "data" / "locomo10.json", encoding="utf8"))):
        for qi, qa in enumerate(conv["qa"]):
            gold[(conv["sample_id"], f"{ci}-{qi}")] = (qa["question"], qa.get("evidence", []), qa["category"])
    return fx, text, gold


def sample(fx, gold, n):
    strata = defaultdict(list)
    for k in sorted(fx):
        if k in gold and gold[k][1] and gold[k][0] == fx[k]["query"]:
            strata[gold[k][2]].append(k)
    total = sum(map(len, strata.values()))
    rng = random.Random(SEED)
    out = []
    for c in sorted(strata):
        out += rng.sample(strata[c], round(n * len(strata[c]) / total))
    return out


def make_scorer(name):
    model, kind = CANDS[name]
    if kind == "qwen":
        from memspine.services.rerank.qwen3_rerank import Qwen3Reranker

        r = Qwen3Reranker(model, device="cpu", batch_size=16)
        r._ensure_loaded()
        return r.score
    if kind == "jina":
        from memspine.services.rerank.jina_rerank import JinaReranker

        return JinaReranker(model, device="cpu").score
    from sentence_transformers import CrossEncoder

    ce = CrossEncoder(model, device="cpu", max_length=512)
    return lambda q, docs: [float(s) for s in ce.predict([(q, d) for d in docs], batch_size=16)]


def cmd_score(a):
    OUT.mkdir(parents=True, exist_ok=True)
    fx, text, gold = load(a.run)
    keys = sample(fx, gold, a.n)
    scorer = make_scorer(a.cand)
    path = OUT / f"{a.run}.{a.cand}.json"
    res = json.loads(path.read_text()) if path.exists() else {}
    res.setdefault("scores", {})
    res.setdefault("pairs", 0)
    res.setdefault("secs", 0.0)
    scorer(fx[keys[0]]["query"], [HEADER + "warm up"] * 2)  # load weights outside the timing
    for i, k in enumerate(keys):
        kid = f"{k[0]}|{k[1]}"
        if kid in res["scores"]:
            continue
        d = fx[k]
        docs = [HEADER + text.get((k[0], p["turn"]), "") for p in d["pool"]]
        t = time.time()
        s = scorer(d["query"], docs)
        res["secs"] += time.time() - t
        res["pairs"] += len(docs)
        res["scores"][kid] = [float(x) for x in s]
        if i % 10 == 0:
            path.write_text(json.dumps(res))
            print(a.cand, i, len(keys), f"{res['pairs'] / res['secs']:.1f} pairs/s", flush=True)
    path.write_text(json.dumps(res))
    print("done", a.cand, f"{res['pairs'] / res['secs']:.2f} pairs/s CPU")


def cmd_report(a):
    from scipy.stats import kendalltau, spearmanr

    fx, text, gold = load(a.run)
    keys = sample(fx, gold, a.n)
    names = [c for c in CANDS if (OUT / f"{a.run}.{c}.json").exists()]
    data = {c: json.loads((OUT / f"{a.run}.{c}.json").read_text()) for c in names}
    common = [k for k in keys if all(f"{k[0]}|{k[1]}" in data[c]["scores"] for c in names)]
    mp = sum(len(fx[k]["pool"]) for k in common) / len(common)
    print(f"questions scored by every candidate: {len(common)} of {len(keys)}; mean pool {mp:.1f}")

    def top(scores, pool, k=10):
        order = sorted(range(len(pool)), key=lambda i: -scores[i])  # stable: ties keep fusion order
        return [pool[i]["turn"] for i in order[:k]]

    def metrics(get, ks=common):
        sp, kt, ov, rec, hit = [], [], [], [], []
        for k in ks:
            d = fx[k]
            pool = d["pool"]
            ref = [x["score"] for x in d["rerank_scores"]]
            sc = get(k)
            t10, r10 = top(sc, pool), top(ref, pool)
            ev = set(gold[k][1])
            ov.append(len(set(t10) & set(r10)) / max(len(r10), 1))
            rec.append(len(ev & set(t10)) / len(ev))
            hit.append(float(bool(ev & set(t10))))
            if len(pool) > 2 and len(set(sc)) > 1 and len(set(ref)) > 1:
                sp.append(spearmanr(sc, ref)[0])
                kt.append(kendalltau(sc, ref)[0])
        m = lambda v: sum(v) / len(v) if v else float("nan")
        return m(sp), m(kt), m(ov), m(rec), m(hit)

    ref4 = lambda k: [x["score"] for x in fx[k]["rerank_scores"]]
    rows = [("pool order (no reranker)", lambda k: [-i for i in range(len(fx[k]["pool"]))], None),
            ("Qwen3-Reranker-4B 4-bit (reference)", ref4, None)]
    for c in names:
        rows.append((CANDS[c][0], (lambda k, c=c: data[c]["scores"][f"{k[0]}|{k[1]}"]), data[c]["pairs"] / data[c]["secs"]))
    ceil = sum(len(set(gold[k][1]) & {p["turn"] for p in fx[k]["pool"]}) / len(gold[k][1]) for k in common) / len(common)
    print(f"pool recall (ceiling) {ceil:.3f}\n")
    print("| Reranker | Spearman vs 4B | Kendall vs 4B | top-10 overlap | gold recall@10 | any-gold hit@10 | CPU pairs/s |")
    print("|---|---|---|---|---|---|---|")
    for n, get, pps in rows:
        sp, kt, ov, rec, hit = metrics(get)
        print(f"| {n} | {sp:.3f} | {kt:.3f} | {ov:.3f} | {rec:.3f} | {hit:.3f} | {'' if pps is None else f'{pps:.1f}'} |")
    # per-category recall and paired bootstrap of recall@10 difference vs 4B
    import random as _r

    rng = _r.Random(1)
    print("\nPaired bootstrap, gold recall@10 minus the 4B's (95% CI):")
    for n, get, _ in rows[2:]:
        diffs = []
        for k in common:
            ev = set(gold[k][1])
            a_ = len(ev & set(top(get(k), fx[k]["pool"]))) / len(ev)
            b_ = len(ev & set(top(ref4(k), fx[k]["pool"]))) / len(ev)
            diffs.append(a_ - b_)
        bs = sorted(sum(rng.choices(diffs, k=len(diffs))) / len(diffs) for _ in range(2000))
        print(f"- {n}: {sum(diffs) / len(diffs):+.3f} [{bs[50]:+.3f}, {bs[1950]:+.3f}]")
    # tie-aware agreement: how many of the 4B's confident documents (P(yes) >= 0.5, at most its top 10) does the
    # candidate's top 10 contain? Plain top-10 overlap is depressed when the 4B ties many documents near 1.0.
    conf_n = [sum(1 for x in fx[k]["rerank_scores"] if x["score"] >= 0.5) for k in common]
    print("\n4B-confident recall (share of the 4B's documents with P(yes) >= 0.5, capped at its top 10, found in the "
          "candidate's top 10):")
    print(f"- 4B marks >=0.5: mean {sum(conf_n) / len(conf_n):.1f} per pool, more than 10 in "
          f"{sum(c > 10 for c in conf_n)} questions, none in {sum(c == 0 for c in conf_n)}")
    for n, get, _ in rows:
        v = []
        for k in common:
            ref = ref4(k)
            idx = [i for i in sorted(range(len(ref)), key=lambda i: -ref[i])[:10] if ref[i] >= 0.5]
            if idx:
                t10 = set(top(get(k), fx[k]["pool"]))
                v.append(sum(fx[k]["pool"][i]["turn"] in t10 for i in idx) / len(idx))
        print(f"- {n}: {sum(v) / len(v):.3f} (n={len(v)})")
    print("\nBy category (gold recall@10): " + ", ".join(f"cat{c}" for c in (1, 2, 3, 4)))
    for n, get, _ in rows:
        out = []
        for c in (1, 2, 3, 4):
            ks = [k for k in common if gold[k][2] == c]
            out.append(f"{metrics(get, ks)[3]:.3f} (n={len(ks)})")
        print(f"- {n}: " + ", ".join(out))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["score", "report"])
    ap.add_argument("cand", nargs="?")
    ap.add_argument("--n", type=int, default=150)
    ap.add_argument("--run", default="r7-protect-full")
    a = ap.parse_args()
    {"score": cmd_score, "report": cmd_report}[a.cmd](a)
