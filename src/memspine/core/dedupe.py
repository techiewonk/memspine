"""I31: near-duplicate removal among the candidates, before assembly.

Several rewordings of one statement (a repeated turn, a mined fact next to its source,
an ingest run twice) each take a slot of the token budget. ``read.dedupe`` removes the
repeats from the candidate list and says what it removed.

Modes (portable units, no corpus-fitted number):

- ``exact``: equal text after case-folding and whitespace normalisation;
- ``jaccard``: Jaccard of the content-word sets at or above ``threshold`` (default 0.9);
- ``embedding``: cosine of the record vectors at or above ``threshold``; the caller
  supplies the vectors (``vectors``), a record without one is compared by ``jaccard``.

Of a duplicate pair the ``keep`` rule chooses the survivor: ``best`` the higher-scored
copy, ``earliest`` the one with the earlier event time (it then takes the better score
of the two, so the pair is not demoted). The pinned persona is never touched. A pure
function of its inputs: the same list always gives the same result.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from memspine.core.query_shape import content_words
from memspine.core.records import MemoryRecord

__all__ = ["DedupeDrop", "dedupe_scored"]


@dataclass(frozen=True)
class DedupeDrop:
    """One removed candidate: its id, the id that stayed, and their similarity."""

    dropped: str
    kept: str
    similarity: float


def _norm(text: str) -> str:
    return " ".join(text.casefold().split())


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dedupe_scored(
    scored: Sequence[tuple[MemoryRecord, float]],
    mode: str,
    *,
    threshold: float = 0.9,
    keep: str = "best",
    vectors: Mapping[str, Sequence[float]] | None = None,
) -> tuple[list[tuple[MemoryRecord, float]], list[DedupeDrop]]:
    """``scored`` without near-duplicates, and the list of what was dropped.

    Order is the input order (a survivor keeps the position of the first copy seen).
    ``mode`` ``off`` returns the input unchanged."""
    items = list(scored)
    if mode == "off" or len(items) < 2:
        return items, []
    texts = [_norm(r.content) for r, _ in items]
    words = [content_words(r.content) for r, _ in items]
    vecs = vectors or {}

    def similarity(i: int, j: int) -> float:
        if mode == "exact":
            return 1.0 if texts[i] and texts[i] == texts[j] else 0.0
        if mode == "embedding":
            a, b = vecs.get(items[i][0].record_id), vecs.get(items[j][0].record_id)
            if a is not None and b is not None:
                return _cosine(a, b)
        return _jaccard(words[i], words[j])

    min_sim = 1.0 if mode == "exact" else threshold
    survivors: list[int] = []  # indexes into ``items``, input order
    replaced: dict[int, tuple[MemoryRecord, float]] = {}
    drops: list[DedupeDrop] = []
    for i, (record, score) in enumerate(items):
        if record.source.channel == "persona":
            survivors.append(i)
            continue
        twin = next(
            (
                j
                for j in survivors
                if items[j][0].source.channel != "persona" and similarity(i, j) >= min_sim
            ),
            None,
        )
        if twin is None:
            survivors.append(i)
            continue
        sim = similarity(i, twin)
        held_record, held_score = replaced.get(twin, items[twin])
        if keep == "earliest" and record.valid_from < held_record.valid_from:
            replaced[twin] = (record, max(score, held_score))
            drops.append(DedupeDrop(held_record.record_id, record.record_id, sim))
        else:
            if keep == "earliest" and score > held_score:
                replaced[twin] = (held_record, score)
            drops.append(DedupeDrop(record.record_id, held_record.record_id, sim))
    return [replaced.get(i, items[i]) for i in survivors], drops
