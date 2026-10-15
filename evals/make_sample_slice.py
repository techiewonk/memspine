"""Small stratified, seeded SAMPLE slices for the sample-first screens (RUNBOOK2, owner decision 2026-10-13).

    python make_sample_slice.py --set loc [--n 80] [--seed 20261013] [--runs-dir DIR] [--source-run clean-jina-full]
    python make_sample_slice.py --set opb    [--n 60] ...                                [--source-run r7-protect-opb]

The question universe comes from a finished full run's ``results.jsonl`` (only ids, category label and conversation are
read; no question, gold or answer text is copied). Output: ``analysis/sample_slice_<set>_<n>_s<seed>.json``:

    slice_id        "sample-<set>-<n>-s<seed>"        (recorded as the manifest slice_id)
    hash            sha256 of the sorted ids           (full hex)
    manifest_hash   the 16-hex slice_hash the run manifest computes from the same ids
    query_ids       "<item_id>/<query_id>" in dataset order (``--query-ids @file`` takes this file)
    category        {id: category label}

``--convs K`` keeps K seeded conversations first (for the engine-LLM screens whose time is ingest, not questions).
Sampling is deterministic from (source ids, n, seed) and does not depend on row order: (1) every category gets a floor
(``--min-per-category``, default 6, or its whole population if smaller) so no category vanishes; (2) the rest is
allocated to categories in proportion to their size (largest remainder); (3) inside a category the quota is spread
over conversations proportionally, again by largest remainder, so every conversation with questions in that category
is represented when the quota allows; (4) a seeded draw picks the ids inside each category x conversation cell.
Blind sets (MAB-CR, ConvoMem, PrefEval) are never sampled: a sample is for iteration, a blind set is seen once.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SEED = 20261013
DEFAULT_N = {"loc": 80, "opb": 60}
DEFAULT_SOURCE = {"loc": "clean-jina-full", "opb": "r7-protect-opb"}


def manifest_hash(ids: list[str]) -> str:
    """The same formula as ``memspine_evals.manifest.build_identity`` (sha256 of the sorted unique ids, 16 hex)."""
    return hashlib.sha256("\n".join(sorted(set(ids))).encode("utf-8")).hexdigest()[:16]


def digest(ids: list[str]) -> str:
    return hashlib.sha256("\n".join(sorted(ids)).encode("utf-8")).hexdigest()


def largest_remainder(weights: dict, total: int, caps: dict | None = None) -> dict:
    """Integer allocation of ``total`` proportional to ``weights``; each key capped by ``caps`` (default its weight)."""
    caps = caps or weights
    w_sum = sum(weights.values()) or 1
    quota = {k: total * v / w_sum for k, v in weights.items()}
    alloc = {k: min(int(quota[k]), caps[k]) for k in weights}
    while sum(alloc.values()) < total:
        room = sorted((k for k in weights if alloc[k] < caps[k]), key=str)
        if not room:
            break
        k = max(
            room, key=lambda k: quota[k] - alloc[k]
        )  # first of equals: sorted order, so deterministic
        alloc[k] += 1
    return alloc


def stratified_sample(
    records: list[tuple[str, str, str]], n: int, seed: int, min_per_category: int = 6
) -> list[str]:
    """``records`` = (id, category, conversation). Returns the sampled ids (sorted). See the module docstring."""
    by_cat: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for qid, cat, conv in records:
        by_cat[cat][conv].append(qid)
    cats = sorted(by_cat)
    size = {c: sum(len(v) for v in by_cat[c].values()) for c in cats}
    n = min(n, sum(size.values()))
    floor = {c: min(min_per_category, size[c]) for c in cats}
    if sum(floor.values()) > n:  # tiny n: spread the floor evenly instead
        floor = largest_remainder({c: 1 for c in cats}, n, caps=size)
    extra = largest_remainder(
        {c: size[c] - floor[c] for c in cats},
        n - sum(floor.values()),
        caps={c: size[c] - floor[c] for c in cats},
    )
    rng = random.Random(seed)
    out: list[str] = []
    for c in cats:
        quota = floor[c] + extra[c]
        convs = sorted(by_cat[c])
        per = largest_remainder({v: len(by_cat[c][v]) for v in convs}, quota)
        for v in convs:
            out += rng.sample(sorted(by_cat[c][v]), per[v])
    return sorted(out)


def read_universe(run_dir: Path, which: str) -> tuple[list[str], list[tuple[str, str, str]]]:
    """(ids in dataset order, (id, category, conversation) records) from a finished run's results.jsonl."""
    order, recs = [], []
    for line in (run_dir / "results.jsonl").open(encoding="utf-8"):
        d = json.loads(line)
        if d.get("kind") != "result":
            continue
        qid = f"{d['item_id']}/{d['query_id']}"
        cat = str(d.get("type_label") or "?")
        if which == "opb":
            cat = cat.split("/", 1)[
                0
            ]  # sycophancy/fact -> sycophancy: keeps the strata few and large
        order.append(qid)
        recs.append((qid, cat, str(d["item_id"])))
    return order, recs


def build(
    which: str,
    runs: Path,
    source_run: str,
    n: int,
    seed: int,
    min_per_category: int = 6,
    convs: int | None = None,
) -> dict:
    order, recs = read_universe(runs / f"{source_run}--memspine", which)
    if convs:  # engine-LLM screens are ingest-bound: restrict to K seeded conversations so only those are ingested
        keep = set(random.Random(seed).sample(sorted({c for _, _, c in recs}), convs))
        recs = [r for r in recs if r[2] in keep]
    picked = set(stratified_sample(recs, n, seed, min_per_category))
    ids = [q for q in order if q in picked]
    cat = {q: c for q, c, _ in recs if q in picked}
    sizes: dict[str, int] = defaultdict(int)
    for c in cat.values():
        sizes[c] += 1
    return {
        "benchmark": "locomo" if which == "loc" else "op_bench",
        "slice_id": f"sample-{which}-{len(ids)}-s{seed}" + (f"-c{convs}" if convs else ""),
        "conversations": sorted({c for q, _, c in recs if q in picked}),
        "kind": "sample",
        "source_run": source_run,
        "seed": seed,
        "stratify": f"category floor {min_per_category}, then proportional; category x conversation, largest remainder",
        "n_total": len(order),
        "n": len(ids),
        "sizes": dict(sorted(sizes.items())),
        "hash": digest(ids),
        "manifest_hash": manifest_hash(ids),
        "category": {q: cat[q] for q in ids},
        "query_ids": ids,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--set", dest="which", choices=sorted(DEFAULT_N), required=True)
    ap.add_argument("--n", type=int, default=None)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--min-per-category", type=int, default=6)
    ap.add_argument(
        "--convs",
        type=int,
        default=None,
        help="restrict to K seeded conversations (ingest-bound screens)",
    )
    ap.add_argument("--source-run", default=None)
    ap.add_argument("--runs-dir", type=Path, default=HERE / "runs")
    ap.add_argument("--out-dir", type=Path, default=HERE / "analysis")
    a = ap.parse_args()
    obj = build(
        a.which,
        a.runs_dir,
        a.source_run or DEFAULT_SOURCE[a.which],
        a.n or DEFAULT_N[a.which],
        a.seed,
        a.min_per_category,
        a.convs,
    )
    name = (
        f"sample_slice_{a.which}_{obj['n']}_s{a.seed}"
        + (f"_c{a.convs}" if a.convs else "")
        + ".json"
    )
    (a.out_dir / name).write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8")
    print(name, obj["n"], "of", obj["n_total"], obj["sizes"], obj["hash"][:12])


if __name__ == "__main__":
    main()
