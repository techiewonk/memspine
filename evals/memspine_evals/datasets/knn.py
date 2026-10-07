"""Retrieval-only kNN scorers for behaviour-log benchmarks (LaMP-2, MemoryCD).

These turn a *ranked list of retrieved turn ids* into a prediction without a reader:
the labels (LaMP-2 tags) or values (MemoryCD ratings) of the top-k retrieved profile
items vote. The prediction is a free, deterministic proxy for "does retrieval surface the
user's own relevant history"; it is not the benchmark's official (reader-based) metric.

All functions are pure: ids missing from the label map (or duplicate ids) are skipped, so
a system that returns non-profile evidence is not credited for it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from statistics import fmean

__all__ = ["knn_mean", "knn_vote", "mae", "vote_accuracy"]


def _top(retrieved_ids: Sequence[str], known: Mapping[str, object], k: int) -> list[str]:
    seen: list[str] = []
    for tid in retrieved_ids:
        if tid in known and tid not in seen:
            seen.append(tid)
            if len(seen) >= k:
                break
    return seen


def knn_vote(retrieved_ids: Sequence[str], labels: Mapping[str, str], k: int) -> str | None:
    """Majority label of the top-``k`` labelled retrieved ids; ``None`` if none is labelled.

    Ties break by rank: the tied label whose best-ranked voter is earliest wins, so the
    result is deterministic and a 1-NN vote is a special case."""
    top = _top(retrieved_ids, labels, k)
    if not top:
        return None
    counts: dict[str, int] = {}
    first: dict[str, int] = {}
    for rank, tid in enumerate(top):
        label = labels[tid]
        counts[label] = counts.get(label, 0) + 1
        first.setdefault(label, rank)
    return min(counts, key=lambda lab: (-counts[lab], first[lab]))


def knn_mean(retrieved_ids: Sequence[str], values: Mapping[str, float], k: int) -> float | None:
    """Mean value of the top-``k`` valued retrieved ids; ``None`` if none has a value."""
    top = _top(retrieved_ids, values, k)
    return fmean(values[t] for t in top) if top else None


def vote_accuracy(predictions: Sequence[str | None], golds: Sequence[str]) -> float:
    """Share of exact (case-insensitive) matches; an abstained ``None`` counts as wrong."""
    if not golds:
        return 0.0
    hits = sum(
        1
        for p, g in zip(predictions, golds, strict=True)
        if p is not None and p.strip().lower() == g.strip().lower()
    )
    return hits / len(golds)


def mae(
    predictions: Sequence[float | None], golds: Sequence[float], fallback: float | None = None
) -> float:
    """Mean absolute error. A ``None`` prediction uses ``fallback`` (e.g. the user's mean)
    or, when no fallback is given, is excluded; an all-excluded set raises."""
    errors = [
        abs((p if p is not None else fallback) - g)  # type: ignore[operator]
        for p, g in zip(predictions, golds, strict=True)
        if p is not None or fallback is not None
    ]
    if not errors:
        raise ValueError("no scorable predictions")
    return fmean(errors)
