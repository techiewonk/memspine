"""N21 (plan v3.2, RAGDefender / TrustRAG): collapse a dense near-duplicate cluster.

Corpus poisoning plants several paraphrases of one false claim so that together they
outvote the truth in the top-k (PoisonedRAG plants five per target question). Honest
evidence rarely repeats itself that tightly. A candidate with at least
``min_cluster - 1`` others sharing ``jaccard`` of its content words belongs to a dense
cluster; each such cluster is collapsed to its best-scored member, which is tagged
``concentrated:<n>`` so the reader (and an auditor) can see that n near-identical
records stood behind it. Lexical, no model.

Free calibration (2026-10-07, content-word Jaccard 0.2, clusters of 3+): PoisonedRAG
planted passages 257 / 300 questions; LoCoMo 10-turn windows 8 / 581.
"""

from __future__ import annotations

from collections.abc import Sequence

from memspine.core.query_shape import content_words
from memspine.core.records import MemoryRecord

__all__ = ["CONCENTRATED_PREFIX", "collapse_concentrated"]

CONCENTRATED_PREFIX = "concentrated:"


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def collapse_concentrated(
    scored: Sequence[tuple[MemoryRecord, float]],
    *,
    jaccard: float = 0.2,
    min_cluster: int = 3,
) -> list[tuple[MemoryRecord, float]]:
    """``scored`` with every dense near-duplicate cluster reduced to its best member.

    Order and every other candidate are kept; the pinned persona is never touched."""
    items = list(scored)
    words = [content_words(record.content) for record, _ in items]
    eligible = [record.source.channel != "persona" for record, _ in items]
    n = len(items)
    neighbours: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        if not eligible[i]:
            continue
        for j in range(i + 1, n):
            if eligible[j] and _jaccard(words[i], words[j]) >= jaccard:
                neighbours[i].append(j)
                neighbours[j].append(i)
    dense = {i for i in range(n) if len(neighbours[i]) >= min_cluster - 1}
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in dense:
        for j in neighbours[i]:
            if j in dense:
                parent[find(i)] = find(j)
    clusters: dict[int, list[int]] = {}
    for i in dense:
        clusters.setdefault(find(i), []).append(i)
    drop: set[int] = set()
    keep_tag: dict[int, int] = {}
    for members in clusters.values():
        if len(members) < min_cluster:
            continue
        best = max(members, key=lambda i: (items[i][1], -i))
        keep_tag[best] = len(members)
        drop.update(m for m in members if m != best)
    out: list[tuple[MemoryRecord, float]] = []
    for i, (record, score) in enumerate(items):
        if i in drop:
            continue
        if i in keep_tag:
            tag = f"{CONCENTRATED_PREFIX}{keep_tag[i]}"
            record = record.model_copy(update={"tags": [*record.tags, tag]})
        out.append((record, score))
    return out
