#!/usr/bin/env python
"""Kernel-only benchmark for bounded RRF prefix selection versus full sort."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import json
import os
import platform
import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path

from memspine.config.constants import RRF_K
from memspine.services.lexical.base import LexicalHit, rrf_fuse


def _reference(
    vector: list[LexicalHit], lexical: list[LexicalHit], *, limit: int, k: int = RRF_K
) -> list[tuple[str, float]]:
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
    return sorted(fused.items(), key=lambda item: (-item[1], ranks[item[0]]))[:limit]


def _make_hits(size: int, seed: int) -> tuple[list[LexicalHit], list[LexicalHit]]:
    rng = random.Random(seed)
    id_count = max(1, size * 2 // 3)
    ids = [f"r-{index:06d}" for index in range(id_count)]
    vector_ids = [rng.choice(ids) for _ in range(size)]
    lexical_ids = [rng.choice(ids) for _ in range(size)]
    return (
        [LexicalHit(rid, 1.0) for rid in vector_ids],
        [LexicalHit(rid, 1.0) for rid in lexical_ids],
    )


def _bench(
    baseline: Callable[[], object], candidate: Callable[[], object], label: str
) -> dict[str, object]:
    baseline()
    candidate()
    for _ in range(2):
        baseline()
        candidate()
    start = time.perf_counter()
    baseline()
    loops = max(1, min(100_000, int(0.075 / max(time.perf_counter() - start, 1e-7))))
    base_samples: list[float] = []
    candidate_samples: list[float] = []
    for trial in range(7):
        order = (baseline, candidate) if trial % 2 == 0 else (candidate, baseline)
        results: dict[Callable[[], object], float] = {}
        for fn in order:
            start_ns = time.perf_counter_ns()
            for _ in range(loops):
                fn()
            results[fn] = (time.perf_counter_ns() - start_ns) / loops
        base_samples.append(results[baseline])
        candidate_samples.append(results[candidate])
    baseline_median = statistics.median(base_samples)
    candidate_median = statistics.median(candidate_samples)
    return {
        "case": label,
        "trials": 7,
        "loops_per_trial": loops,
        "baseline_median_us": round(baseline_median / 1000, 3),
        "candidate_median_us": round(candidate_median / 1000, 3),
        "candidate_over_baseline": round(candidate_median / baseline_median, 4),
        "baseline_trial_us": [round(value / 1000, 3) for value in base_samples],
        "candidate_trial_us": [round(value / 1000, 3) for value in candidate_samples],
    }


def run() -> dict[str, object]:
    cases: list[dict[str, object]] = []
    for size in (16, 32, 64, 128, 256, 512, 1024, 2048, 4096):
        vector, lexical = _make_hits(size, 104729 + size)
        fused_count = len({hit.record_id for hit in (*vector, *lexical)})
        for limit in (3, 5, 10, 32):
            expected = _reference(vector, lexical, limit=limit)
            actual = rrf_fuse(vector, lexical, limit=limit)
            if actual != expected:
                raise AssertionError(f"prefix parity failed size={size}, limit={limit}")
            cases.append(
                _bench(
                    lambda vector=vector, lexical=lexical, limit=limit: _reference(
                        vector, lexical, limit=limit
                    ),
                    lambda vector=vector, lexical=lexical, limit=limit: rrf_fuse(
                        vector, lexical, limit=limit
                    ),
                    f"{size}_hits_per_leg_limit_{limit}_unique_{fused_count}",
                )
            )
    try:
        xxhash_version = importlib.metadata.version("xxhash")
    except importlib.metadata.PackageNotFoundError:
        xxhash_version = None
    return {
        "schema_version": 1,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "xxhash": xxhash_version,
            "host_load_controlled": False,
        },
        "settings": {
            "warmups": 2,
            "trials": 7,
            "target_seconds_per_implementation_trial": 0.075,
            "trial_order": "alternating baseline-first and candidate-first",
            "sizes_per_leg": [16, 32, 64, 128, 256, 512, 1024, 2048, 4096],
            "limits": [3, 5, 10, 32],
            "seed_base": 104729,
        },
        "prefix_parity": "exact score and order, checked before each timing",
        "cases": cases,
        "notes": [
            "Compares the frozen full-sort accumulation against the production bounded RRF helper.",
            "These kernel-only synthetic timings do not assert retrieval relevance or end-to-end latency.",
        ],
    }


def main() -> None:
    report = run()
    output = Path("docs/provider_verification/bounded-topk.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output.write_text(raw, encoding="utf-8")
    print(raw, end="")


if __name__ == "__main__":
    main()
