"""LoCoMo category-5 (adversarial) sample slices for the relevance / abstention screens (S20-CAT5).

    python make_cat5_slice.py [--n 80] [--c3-n 30] [--seed 20261013] [--data PATH] [--out-dir analysis]

Category 5 is the set of questions about things that never happened; the right answer is a refusal (gold is
``ABSTENTION_GOLD``, judged by ``ABSTENTION_BINARY_PROMPT``, never the file's ``adversarial_answer``). The category 1-4
sample slices never contain it (their source run has no cat 5), so a relevance gate (S20 ``read.gate_v2``) or any
abstention-related screen has never been measured on it.

Source: ``data/locomo10.json`` (read for the sample order and each question's category only; no question, answer or
distractor text is copied). If ``<evals>/data`` is absent (a worktree) the sibling ``memspine`` checkout is used. Ids are
``<sample_id>/<sample index>-<qa index>``, the same form the harness uses (the qa index counts ALL categories, so the id of
a question does not change when the category filter does).

Outputs under ``analysis/`` (same keys as the other sample slices plus ``categories``):

    sample_slice_loc_cat5_s<seed>.json       ``n`` category-5 questions over all 10 conversations, largest remainder
                                             on the per-conversation cat-5 population, seeded draw inside each
    sample_slice_loc_cat5_s<seed>_c3.json    ``c3-n`` questions over the SAME three conversations as
                                             ``sample_slice_loc_60_s20261013_c3`` (engine-LLM rows are ingest-bound)

Running a slice needs ``--categories 5`` (the dataset filters by category BEFORE ``--query-ids``); the queue passes it
from column 6 (``categories=5``) of ``v2_slice_map.tsv``.
"""

from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from make_sample_slice import digest, largest_remainder, manifest_hash

HERE = Path(__file__).parent
SEED = 20261013
CAT = 5
C3_SOURCE = "analysis/sample_slice_loc_60_s20261013_c3.json"
DATA_CANDIDATES = (
    HERE / "data" / "locomo10.json",
    HERE.parent / "data" / "locomo10.json",
    HERE.parent.parent / "memspine" / "data" / "locomo10.json",
)


def find_data(path: Path | None = None) -> Path:
    for p in [path] if path else list(DATA_CANDIDATES):
        if p and p.exists():
            return p
    raise SystemExit("locomo10.json not found; pass --data")


def read_universe(data: Path, cat: int = CAT) -> tuple[list[str], dict[str, str], int]:
    """(category-``cat`` ids in dataset order, {id: conversation}, total question count over all categories)."""
    raw = json.loads(data.read_text(encoding="utf-8"))
    ids, conv, total = [], {}, 0
    for index, sample_ in enumerate(raw):
        for q_index, qa in enumerate(sample_.get("qa") or []):
            total += 1
            if qa.get("category") == cat:
                q = f"{sample_['sample_id']}/{index}-{q_index}"
                ids.append(q)
                conv[q] = str(sample_["sample_id"])
    return ids, conv, total


def sample(
    order: list[str], conv: dict[str, str], n: int, seed: int, convs: list[str] | None = None
) -> list[str]:
    """Stratify by conversation (largest remainder on the cat-5 population), seeded draw inside each; dataset order out."""
    by: dict[str, list[str]] = {}
    for q in order:
        if convs is None or conv[q] in convs:
            by.setdefault(conv[q], []).append(q)
    n = min(n, sum(len(v) for v in by.values()))
    per = largest_remainder({c: len(v) for c, v in by.items()}, n)
    rng = random.Random(seed)
    picked: set[str] = set()
    for c in sorted(by):
        picked |= set(rng.sample(sorted(by[c]), per[c]))
    return [q for q in order if q in picked]


def build(
    order: list[str],
    conv: dict[str, str],
    total: int,
    n: int,
    seed: int,
    convs: list[str] | None = None,
) -> dict:
    ids = sample(order, conv, n, seed, convs)
    per_conv: dict[str, int] = {}
    for q in ids:
        per_conv[conv[q]] = per_conv.get(conv[q], 0) + 1
    return {
        "benchmark": "locomo",
        "slice_id": f"sample-loc-cat5-{len(ids)}-s{seed}" + ("-c3" if convs else ""),
        "conversations": sorted(per_conv),
        "kind": "sample",
        "family": "cat5",
        "categories": [CAT],
        "categories_arg": str(CAT),
        "category_labels": {"cat5": "adversarial (unanswerable; the correct answer is a refusal)"},
        "source_run": "data/locomo10.json",
        "seed": seed,
        "stratify": (
            "category 5 only; conversation, largest remainder on the cat-5 population, seeded draw inside each"
            + ("; the three conversations of sample_slice_loc_60_s20261013_c3" if convs else "")
        ),
        "reading_rule": "NOT comparable with the category 1-4 slices: read wins/losses against the same-slice reference run "
        "(a win is a correct refusal the reference missed; a loss is a refusal the arm no longer gives)",
        "n_total": total,
        "n_category": len(order),
        "n": len(ids),
        "per_conversation": dict(sorted(per_conv.items())),
        "sizes": {"cat5": len(ids)},
        "hash": digest(ids),
        "manifest_hash": manifest_hash(ids),
        "category": {q: "cat5" for q in ids},
        "query_ids": ids,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--n", type=int, default=80)
    ap.add_argument("--c3-n", type=int, default=30)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--data", type=Path, default=None)
    ap.add_argument("--out-dir", type=Path, default=HERE / "analysis")
    a = ap.parse_args()
    order, conv, total = read_universe(find_data(a.data))
    c3 = json.loads((HERE / C3_SOURCE).read_text(encoding="utf-8"))["conversations"]
    for suffix, obj in (
        ("", build(order, conv, total, a.n, a.seed)),
        ("_c3", build(order, conv, total, a.c3_n, a.seed, c3)),
    ):
        path = a.out_dir / f"sample_slice_loc_cat5_s{a.seed}{suffix}.json"
        path.write_text(json.dumps(obj, indent=1) + "\n", encoding="utf-8", newline="\n")
        print(
            f"{path.name}: n={obj['n']} of {obj['n_category']} cat-5; per conversation {obj['per_conversation']}"
        )


if __name__ == "__main__":
    main()
