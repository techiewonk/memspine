"""RM-37 / LX1-A: bounded offline BM25S vs Tantivy comparison (no runtime provider added).

Fixed tiny local corpus and queries (no downloads, no datasets). Both engines index the
same documents with equivalent tokenization (lower-cased alphanumeric runs, no stemming,
no stop words); the script reports per-query top-k parity (set Jaccard, top-1 agreement,
exact order agreement) and search latency (median / p95 over repeated runs).

Tantivy runs through memspine's own :class:`TantivyLexical` adapter (the default core
lexical leg, in-RAM index). BM25S is optional and NOT a memspine dependency: when
``bm25s`` is not importable the script prints a ``skipped`` status and exits 0.

Usage::

    python scripts/eval_bm25s_vs_tantivy.py [--k 3] [--reps 50] [--out PATH]

``--out`` writes the JSON result (only when the comparison actually ran).
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import platform
import re
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

NAMESPACE = "eval/bm25s"

#: Fixed corpus: short memory-like statements with overlapping vocabulary so ranking matters.
CORPUS: tuple[str, ...] = (
    "Alice moved to Berlin in March and started a new job at a robotics company.",
    "Bob prefers green tea in the morning and coffee after lunch.",
    "The quarterly report is due on Friday; Alice is responsible for the budget section.",
    "Carol adopted a dog named Pepper from the shelter last spring.",
    "Bob and Carol went hiking in the Alps during the summer holiday.",
    "Alice's robotics team shipped the new warehouse robot firmware on Tuesday.",
    "Dave is allergic to peanuts and avoids Thai restaurants.",
    "The budget meeting was moved from Thursday to Friday morning.",
    "Carol is learning Spanish and practises every evening with an app.",
    "Dave started training for the Berlin marathon in the spring.",
    "Bob's coffee machine broke, so he switched to tea for a week.",
    "The warehouse in Hamburg uses robots for sorting parcels at night.",
)

#: Queries chosen to exercise single-term, multi-term and ambiguous matches.
QUERIES: tuple[str, ...] = (
    "Alice robotics job",
    "budget Friday",
    "Bob coffee tea",
    "Carol dog",
    "Berlin marathon",
    "warehouse robots",
    "spring",
    "Spanish evening practice",
)

_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Lower-cased alphanumeric runs: the tokenization of Tantivy's ``default`` analyzer."""
    return [t.lower() for t in _TOKEN.findall(text)]


def _stats(samples_s: list[float]) -> dict[str, float]:
    ordered = sorted(samples_s)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "median_us": round(statistics.median(ordered) * 1e6, 1),
        "p95_us": round(p95 * 1e6, 1),
    }


async def _tantivy_rankings(k: int, reps: int) -> tuple[list[list[str]], dict[str, float], float]:
    from memspine.clients.tantivy import TantivyConnector
    from memspine.services.lexical.tantivy import TantivyLexical, tantivy_spec
    from memspine.store.records import MemoryRecord

    store = TantivyLexical(TantivyConnector(tantivy_spec()))
    records = [
        MemoryRecord(
            namespace=NAMESPACE, memory_type="semantic", content=text, record_id=f"d{i:02d}"
        )
        for i, text in enumerate(CORPUS)
    ]
    start = time.perf_counter()
    await store.index_many(records)
    index_s = time.perf_counter() - start
    rankings = [[h.record_id for h in await store.search(NAMESPACE, q, top_k=k)] for q in QUERIES]
    samples: list[float] = []
    for _ in range(reps):
        for q in QUERIES:
            t0 = time.perf_counter()
            await store.search(NAMESPACE, q, top_k=k)
            samples.append(time.perf_counter() - t0)
    await store.close()
    return rankings, _stats(samples), index_s


def _bm25s_rankings(k: int, reps: int) -> tuple[list[list[str]], dict[str, float], float, str]:
    import bm25s  # type: ignore[import-not-found]

    start = time.perf_counter()
    retriever = bm25s.BM25()
    retriever.index([tokenize(text) for text in CORPUS])
    index_s = time.perf_counter() - start
    kk = min(k, len(CORPUS))

    def search(query: str) -> list[str]:
        docs, scores = retriever.retrieve([tokenize(query)], k=kk)
        return [
            f"d{int(doc):02d}"
            for doc, score in zip(docs[0], scores[0], strict=True)
            if float(score) > 0.0
        ]

    rankings = [search(q) for q in QUERIES]
    samples: list[float] = []
    for _ in range(reps):
        for q in QUERIES:
            t0 = time.perf_counter()
            search(q)
            samples.append(time.perf_counter() - t0)
    return rankings, _stats(samples), index_s, str(getattr(bm25s, "__version__", "unknown"))


def _parity(a: list[list[str]], b: list[list[str]]) -> dict[str, Any]:
    per_query = []
    for query, ra, rb in zip(QUERIES, a, b, strict=True):
        union = set(ra) | set(rb)
        jaccard = len(set(ra) & set(rb)) / len(union) if union else 1.0
        per_query.append(
            {
                "query": query,
                "tantivy": ra,
                "bm25s": rb,
                "jaccard": round(jaccard, 3),
                "top1_agree": (ra[:1] == rb[:1]),
                "order_agree": ra == rb,
            }
        )
    n = len(per_query)
    return {
        "mean_jaccard": round(sum(q["jaccard"] for q in per_query) / n, 3),
        "top1_agreement": round(sum(q["top1_agree"] for q in per_query) / n, 3),
        "exact_order_agreement": round(sum(q["order_agree"] for q in per_query) / n, 3),
        "per_query": per_query,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--k", type=int, default=3)
    parser.add_argument("--reps", type=int, default=50)
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)

    base: dict[str, Any] = {
        "task": "RM-37",
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "corpus_docs": len(CORPUS),
        "queries": len(QUERIES),
        "k": args.k,
        "reps": args.reps,
    }
    if importlib.util.find_spec("bm25s") is None:
        base |= {
            "status": "skipped",
            "reason": "bm25s is not installed (not a memspine dependency)",
        }
        print(json.dumps(base, indent=2))
        return 0

    tantivy_rank, tantivy_lat, tantivy_index_s = asyncio.run(_tantivy_rankings(args.k, args.reps))
    bm25s_rank, bm25s_lat, bm25s_index_s, bm25s_version = _bm25s_rankings(args.k, args.reps)
    result = base | {
        "status": "ran",
        "bm25s_version": bm25s_version,
        "parity": _parity(tantivy_rank, bm25s_rank),
        "latency": {
            "tantivy": tantivy_lat | {"index_ms": round(tantivy_index_s * 1e3, 2)},
            "bm25s": bm25s_lat | {"index_ms": round(bm25s_index_s * 1e3, 2)},
        },
        "note": "tiny fixed corpus: a parity/latency smoke, not a quality benchmark",
    }
    text = json.dumps(result, indent=2)
    print(text)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
