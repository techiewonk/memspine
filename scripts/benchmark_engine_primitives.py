#!/usr/bin/env python
"""Bounded comparisons for existing Memspine CPU hot paths.

The harness is deliberately local and dependency-free beyond the repository's
already-installed runtime. It reports timings only; it does not alter defaults
or claim end-to-end retrieval quality. Run with PYTHONPATH=src and the normal
Python 3.13 environment.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import heapq
import importlib.metadata
import json
import os
import platform
import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path

import xxhash

from memspine.core.policies.dedup import DedupPolicy
from memspine.services.embedding.hash import HashEmbedding
from memspine.services.lexical.base import LexicalHit, rrf_fuse


def _original_simhash(text: str) -> int:
    """Frozen pre-optimization implementation used as a parity/timing oracle."""
    weights = [0] * 64
    for token in text.lower().split():
        digest = xxhash.xxh64_intdigest(token.encode("utf-8"))
        for bit in range(64):
            weights[bit] += 1 if (digest >> bit) & 1 else -1
    value = sum(1 << bit for bit, weight in enumerate(weights) if weight > 0)
    return value - (1 << 64) if value >= (1 << 63) else value


def _rrf_topk(
    vector: list[LexicalHit], lexical: list[LexicalHit], top_k: int, k: int = 60
) -> list[tuple[str, float]]:
    """Partial-sort candidate using the same accumulation and tie-break rules."""
    legs = (vector, lexical)
    fused: dict[str, float] = {}
    ranks: dict[str, list[int]] = {}
    absent = 1 + max(map(len, legs), default=0)
    for leg, hits in enumerate(legs):
        for rank, hit in enumerate(hits, 1):
            rid = hit.record_id
            slots = ranks.setdefault(rid, [absent] * len(legs))
            slots[leg] = min(slots[leg], rank)
            fused[rid] = fused.get(rid, 0.0) + 1.0 / (k + rank)
    key = lambda item: (-item[1], ranks[item[0]])
    return heapq.nsmallest(top_k, fused.items(), key=key)


def _digest_environment() -> dict[str, object]:
    packages = (
        "xxhash",
        "datasketch",
        "orjson",
        "zstandard",
        "numpy",
        "lancedb",
        "tantivy",
        "onnxruntime",
        "fastembed",
    )
    versions: dict[str, str | None] = {}
    for package in packages:
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    try:
        cpu_count = os.cpu_count()
    except Exception:
        cpu_count = None
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cpu_count": cpu_count,
        "packages": versions,
        "model_loaded": False,
        "network_used": False,
    }


def _bench_pair(
    label: str,
    baseline: Callable[[], object],
    candidate: Callable[[], object],
    *,
    warmups: int = 2,
    trials: int = 7,
    target_trial_s: float = 0.12,
) -> dict[str, object]:
    """Time matched repetitions with alternating order and return medians."""
    baseline()
    candidate()
    for _ in range(warmups):
        baseline()
        candidate()
    # Calibrate one unit to a bounded, stable run duration.
    start = time.perf_counter()
    baseline()
    one = max(time.perf_counter() - start, 1e-7)
    loops = max(1, min(100_000, int(target_trial_s / one)))
    base_samples: list[float] = []
    cand_samples: list[float] = []
    for trial in range(trials):
        order = (baseline, candidate) if trial % 2 == 0 else (candidate, baseline)
        measured: dict[Callable[[], object], float] = {}
        for fn in order:
            start = time.perf_counter_ns()
            for _ in range(loops):
                fn()
            measured[fn] = (time.perf_counter_ns() - start) / loops
        base_samples.append(measured[baseline])
        cand_samples.append(measured[candidate])
    base_med = statistics.median(base_samples)
    cand_med = statistics.median(cand_samples)
    return {
        "case": label,
        "trials": trials,
        "loops_per_trial": loops,
        "target_trial_seconds": target_trial_s,
        "baseline_median_us": round(base_med / 1000, 3),
        "candidate_median_us": round(cand_med / 1000, 3),
        "candidate_over_baseline": round(cand_med / base_med, 4),
        "baseline_trial_us": [round(value / 1000, 3) for value in base_samples],
        "candidate_trial_us": [round(value / 1000, 3) for value in cand_samples],
    }


def _make_hits(size: int, seed: int) -> tuple[list[LexicalHit], list[LexicalHit]]:
    rng = random.Random(seed)
    ids_a = [f"r{index:06d}" for index in range(size)]
    ids_b = ids_a[:]
    rng.shuffle(ids_a)
    rng.shuffle(ids_b)
    # Restrict each leg to a realistic retrieved candidate window, then union.
    return (
        [LexicalHit(record_id, 1.0) for record_id in ids_a[:size]],
        [LexicalHit(record_id, 1.0) for record_id in ids_b[:size]],
    )


def _batch_probe() -> dict[str, object]:
    """Measure actual HashEmbedding calls versus one batched call (same vectors)."""
    embedder = HashEmbedding(dim=384)
    inputs = [f"memory event {i} alice discussed project {i % 13}" for i in range(64)]
    expected = asyncio.run(embedder.embed(inputs))
    batches = [1, 8, 32, 64]
    results: list[dict[str, object]] = []
    for batch_size in batches:
        # Keep each trial matched on one 64-item logical workload. Baseline
        # makes 64 one-item calls; candidate makes four/eight/etc batch calls.
        def separate() -> list[list[float]]:
            async def run() -> list[list[float]]:
                out: list[list[float]] = []
                for text in inputs:
                    out.extend(await embedder.embed([text]))
                return out

            return asyncio.run(run())

        def batched(batch_size: int = batch_size) -> list[list[float]]:
            async def run() -> list[list[float]]:
                out: list[list[float]] = []
                for start in range(0, len(inputs), batch_size):
                    out.extend(await embedder.embed(inputs[start : start + batch_size]))
                return out

            return asyncio.run(run())

        candidate = batched()
        if candidate != expected:
            raise AssertionError(f"HashEmbedding batch parity failed for batch_size={batch_size}")
        timing = _bench_pair(
            f"hash_embedding_64_items_batch_{batch_size}",
            separate,
            batched,
            trials=5,
            target_trial_s=0.04,
        )
        results.append(timing)
    return {"results": results, "vector_parity": True, "semantic_quality_claim": False}


def run() -> dict[str, object]:
    policy = DedupPolicy.bind()
    simhash_results: list[dict[str, object]] = []
    for count in (1, 2, 4, 8, 16, 64, 512, 2048, 8193):
        text = " ".join(f"token-{i % 701}" for i in range(count))
        expected = _original_simhash(text)
        actual = policy.simhash64(text)
        if actual != expected:
            raise AssertionError(f"production SimHash parity failed for {count} tokens")
        simhash_results.append(
            _bench_pair(
                f"simhash_{count}_tokens",
                lambda text=text: _original_simhash(text),
                lambda text=text: policy.simhash64(text),
                target_trial_s=0.10,
            )
        )

    rrf_results: list[dict[str, object]] = []
    for size in (16, 64, 256, 1024):
        vector, lexical = _make_hits(size, 8119 + size)
        reference = rrf_fuse(vector, lexical)
        for top_k in (5, 10):
            expected = reference[:top_k]
            actual = _rrf_topk(vector, lexical, top_k)
            if actual != expected:
                raise AssertionError(
                    f"RRF partial-sort parity failed for size={size}, top_k={top_k}"
                )
            rrf_results.append(
                _bench_pair(
                    f"rrf_{size}_per_leg_top_{top_k}",
                    lambda vector=vector, lexical=lexical, top_k=top_k: rrf_fuse(vector, lexical)[
                        :top_k
                    ],
                    lambda vector=vector, lexical=lexical, top_k=top_k: _rrf_topk(
                        vector, lexical, top_k
                    ),
                    target_trial_s=0.10,
                )
            )

    return {
        "schema_version": 1,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "environment": _digest_environment(),
        "settings": {
            "warmups": 2,
            "trials": 7,
            "simhash_cases_tokens": [1, 2, 4, 8, 16, 64, 512, 2048, 8193],
            "simhash_chunk_tokens": 4096,
            "rrf_per_leg_candidates": [16, 64, 256, 1024],
            "rrf_top_k": [5, 10],
            "seed": 8119,
            "trial_order": "alternating baseline-first and candidate-first",
            "host_load_controlled": False,
        },
        "simhash": {"exact_bit_parity": True, "results": simhash_results},
        "rrf": {"exact_order_and_score_parity": True, "results": rrf_results},
        "hash_embedding_batches": _batch_probe(),
        "notes": [
            "Timings are this host's bounded microbenchmark, not service-level latency or production throughput.",
            "The event fingerprint and stable table/cache identifiers remain unchanged.",
            "No cryptographic integrity primitive was benchmarked or replaced.",
            "No model was loaded and no network access was used.",
        ],
    }


def main() -> None:
    report = run()
    output = Path("docs/provider_verification/engine-primitives-benchmark.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
