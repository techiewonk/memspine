#!/usr/bin/env python
"""Prototype bounded datasketch MinHash.update_batch against current updates."""

from __future__ import annotations

import datetime as dt
import importlib.metadata
import itertools
import json
import os
import platform
import random
import statistics
import time
from collections.abc import Callable
from pathlib import Path

from datasketch import MinHash, MinHashLSH

from memspine.config import constants


def _current(text: str, num_perm: int) -> MinHash:
    sketch = MinHash(num_perm=num_perm)
    for token in set(text.lower().split()):
        sketch.update(token.encode())
    return sketch


def _batched(text: str, num_perm: int, max_cells: int) -> MinHash:
    """Prototype with a cap on rows x permutations in update_batch calls."""
    sketch = MinHash(num_perm=num_perm)
    unique_tokens = set(text.lower().split())
    if len(unique_tokens) < constants.MINHASH_BATCH_MIN_UNIQUE_TOKENS:
        for token in unique_tokens:
            sketch.update(token.encode())
        return sketch
    batch_size = max(1, max_cells // num_perm)
    tokens = iter(unique_tokens)
    while batch := list(itertools.islice(tokens, batch_size)):
        sketch.update_batch([token.encode() for token in batch])
    return sketch


def _signature(sketch: MinHash) -> bytes:
    return bytes(sketch.hashvalues.astype("<u8").tobytes())


def _build_text(raw_token_count: int, unique_token_count: int, seed: int) -> str:
    rng = random.Random(seed)
    vocabulary = [f"token-{i}" for i in range(unique_token_count)]
    return " ".join(rng.choice(vocabulary) for _ in range(raw_token_count))


def _bench(
    baseline: Callable[[], object], candidate: Callable[[], object], label: str
) -> dict[str, object]:
    baseline()
    candidate()
    for _ in range(2):
        baseline()
        candidate()
    started = time.perf_counter()
    baseline()
    loops = max(1, min(100_000, int(0.04 / max(time.perf_counter() - started, 1e-7))))
    base_samples: list[float] = []
    candidate_samples: list[float] = []
    for trial in range(7):
        order = (baseline, candidate) if trial % 2 == 0 else (candidate, baseline)
        measured: dict[Callable[[], object], float] = {}
        for fn in order:
            started_ns = time.perf_counter_ns()
            for _ in range(loops):
                fn()
            measured[fn] = (time.perf_counter_ns() - started_ns) / loops
        base_samples.append(measured[baseline])
        candidate_samples.append(measured[candidate])
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


def _check_lsh_parity(num_perm: int, max_cells: int) -> dict[str, object]:
    contents = [
        "alice likes dark coffee in the morning",
        "alice likes strong coffee in the morning",
        "the database uses a local graph index",
        "the database uses a local vector index",
        "東京の会議は木曜日です",
        "東京の会議は金曜日です",
    ]
    baseline = [_current(text, num_perm) for text in contents]
    batched = [_batched(text, num_perm, max_cells) for text in contents]
    if any(_signature(a) != _signature(b) for a, b in zip(baseline, batched, strict=True)):
        raise AssertionError(f"LSH fixture signature mismatch for num_perm={num_perm}")
    left = MinHashLSH(threshold=0.3, num_perm=num_perm)
    right = MinHashLSH(threshold=0.3, num_perm=num_perm)
    for index, (a, b) in enumerate(zip(baseline, batched, strict=True)):
        left.insert(f"id-{index}", a)
        right.insert(f"id-{index}", b)
    query = "alice prefers dark coffee early each morning"
    left_candidates = sorted(left.query(_current(query, num_perm)))
    right_candidates = sorted(right.query(_batched(query, num_perm, max_cells)))
    if left_candidates != right_candidates:
        raise AssertionError(f"LSH candidate mismatch for num_perm={num_perm}")
    return {
        "num_perm": num_perm,
        "signature_bytes_identical": True,
        "restored_signature_jaccard": min(
            MinHash(num_perm=num_perm, hashvalues=a.hashvalues, scheme=a.scheme).jaccard(
                MinHash(num_perm=num_perm, hashvalues=b.hashvalues, scheme=b.scheme)
            )
            for a, b in zip(baseline, batched, strict=True)
        ),
        "lsh_candidates_identical": True,
        "candidate_ids": left_candidates,
    }


def run() -> dict[str, object]:
    results: list[dict[str, object]] = []
    parities: list[dict[str, object]] = []
    for num_perm in (128, 256, 512):
        for raw_count, unique_count in (
            (0, 0),
            (1, 1),
            (2, 2),
            (4, 4),
            (8, 8),
            (16, 16),
            (16, 1),
            (64, 32),
            (512, 256),
            (2048, 1024),
            (8193, 4096),
        ):
            text = _build_text(raw_count, max(1, unique_count), 17011 + raw_count)
            current = _current(text, num_perm)
            for max_cells in (65_536, 262_144):
                candidate = _batched(text, num_perm, max_cells)
                if _signature(current) != _signature(candidate):
                    raise AssertionError(
                        f"signature mismatch raw={raw_count}, unique={unique_count}, "
                        f"num_perm={num_perm}, max_cells={max_cells}"
                    )
                results.append(
                    _bench(
                        lambda text=text, num_perm=num_perm: _signature(_current(text, num_perm)),
                        lambda text=text, num_perm=num_perm, max_cells=max_cells: _signature(
                            _batched(text, num_perm, max_cells)
                        ),
                        f"raw_{raw_count}_unique_{unique_count}_perm_{num_perm}_cells_{max_cells}",
                    )
                )
        parities.append(_check_lsh_parity(num_perm, 65_536))
    try:
        datasketch_version = importlib.metadata.version("datasketch")
    except importlib.metadata.PackageNotFoundError:
        datasketch_version = None
    try:
        numpy_version = importlib.metadata.version("numpy")
    except importlib.metadata.PackageNotFoundError:
        numpy_version = None
    return {
        "schema_version": 1,
        "created_at_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "cpu_count": os.cpu_count(),
            "datasketch": datasketch_version,
            "numpy": numpy_version,
            "host_load_controlled": False,
        },
        "settings": {
            "trials": 7,
            "target_seconds_per_implementation_trial": 0.04,
            "trial_order": "alternating baseline-first and candidate-first",
            "permutation_counts": [128, 256, 512],
            "raw_unique_token_sizes": [
                [0, 0],
                [1, 1],
                [2, 2],
                [4, 4],
                [8, 8],
                [16, 16],
                [16, 1],
                [64, 32],
                [512, 256],
                [2048, 1024],
                [8193, 4096],
            ],
            "max_temporary_cells": [65_536, 262_144],
            "batch_min_unique_tokens": constants.MINHASH_BATCH_MIN_UNIQUE_TOKENS,
        },
        "signature_parity": "exact bytes, checked before every timed case",
        "results": results,
        "lsh_parity": parities,
        "notes": [
            "Prototype uses existing datasketch MinHash.update_batch and never changes MinHash scheme, seed, hashfunc, or permutation setup.",
            "max_cells bounds rows × num_perm per update_batch call; datasketch may hold multiple arrays of this bounded shape temporarily.",
            "Timings are kernel-only and make no retrieval-quality or end-to-end throughput claim.",
        ],
    }


def main() -> None:
    report = run()
    output = Path("docs/provider_verification/minhash-batch.json")
    output.parent.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(report, indent=2, sort_keys=True) + "\n"
    output.write_text(raw, encoding="utf-8")
    print(raw, end="")


if __name__ == "__main__":
    main()
