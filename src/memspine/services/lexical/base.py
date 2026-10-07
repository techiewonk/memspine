"""LexicalStore port (D-25) + the one canonical RRF fusion helper.

Lexical stores are projections (D0.1): rebuildable from the event log by
re-indexing record content (:class:`LexicalProjector`). The surface is
deliberately minimal — index / search (BM25) / delete (FORGET cascade) /
clear (rebuild) — and namespace-scoped: a query in one namespace must never
surface another's content, exactly as the vector port scopes by namespace.

``rrf_fuse`` lives here (implemented ONCE in the port, D-25): reciprocal-rank
fusion of the vector and lexical legs, with deterministic tie-breaks so the
fused ranking is stable across runs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from memspine.config.constants import RRF_K
from memspine.core.records import MemoryRecord

__all__ = ["LexicalHit", "LexicalStore", "RankedHit", "rrf_fuse"]


@dataclass(frozen=True)
class LexicalHit:
    record_id: str
    score: float  # BM25 relevance (higher = better); scale is backend-specific


class RankedHit(Protocol):
    """The single attribute ``rrf_fuse`` reads — both :class:`LexicalHit` and
    ``VectorHit`` satisfy it, so fusion never couples to a concrete store."""

    @property
    def record_id(self) -> str: ...


@runtime_checkable
class LexicalStore(Protocol):
    async def index(self, record: MemoryRecord) -> None:
        """Index (or re-index) one record's content under its namespace.

        Idempotent: re-delivery of the same WRITE during catch-up/rebuild must
        leave the index in the same state (upsert semantics)."""
        ...

    async def index_many(self, records: Sequence[MemoryRecord]) -> None:
        """Index a sequence of records — a convenience over :meth:`index`, NOT an
        atomic batch (implementations may commit per-record). Idempotent."""
        ...

    async def search(self, namespace: str, query: str, top_k: int = 8) -> list[LexicalHit]:
        """BM25-rank ``query`` within ``namespace`` only. User text is treated
        as data — never as query grammar — so a crafted query cannot break the
        backend's match syntax or reach another namespace."""
        ...

    async def delete(self, record_id: str) -> None:
        """Drop a record from the index (FORGET cascade). Idempotent."""
        ...

    async def exists(self, record_id: str) -> bool:
        """M7 ``forget --verify`` proof: is this record still indexed? The
        lexical index stores raw content, so ``verify_forget`` must inspect it —
        a clean vector/record/log store is not proof if the FTS row survived."""
        ...

    async def clear(self) -> None:
        """Truncate the whole index so a rebuild can replay from seq 0."""
        ...

    async def close(self) -> None:
        """Release any store-held resources (the SQLite backend shares the
        injected client, so this is a no-op there)."""
        ...


def rrf_fuse(
    vector_hits: Sequence[RankedHit],
    lexical_hits: Sequence[RankedHit],
    k: int = RRF_K,
    extra: Sequence[Sequence[RankedHit]] = (),
    weights: Sequence[float] | None = None,
) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion (D-25) of the vector and lexical legs.

    Each list contributes ``1 / (k + rank)`` per record (1-based rank in that
    list); a record present in both legs sums both contributions. Returns
    ``(record_id, fused_score)`` sorted by score descending. Ties break on the
    record's ranks leg by leg (vector leg first; absent from a leg ranks last in
    it), so the order is a function of the legs alone (a record a leg lists twice
    keeps both contributions and its best rank there).

    #87: ties used to break on ``record_id``. Record ids are random uuid4s, so
    two records with equal fused scores (rank 3 + rank 5 against rank 5 + rank 3
    is common) swapped places from one run to the next, and at the ``top_k`` cut
    a different record got in. No two records share a rank within one leg, so
    the rank tuple never ties.

    ``weights`` (N16, plan v3.2): one weight per leg (vector, lexical, then each extra
    leg), multiplying its ``1 / (k + rank)`` contributions. None: all 1 (unchanged).
    """
    legs = (vector_hits, lexical_hits, *extra)
    fused: dict[str, float] = {}
    ranks: dict[str, list[int]] = {}
    absent = 1 + max((len(hits) for hits in legs), default=0)
    for leg, hits in enumerate(legs):
        for rank, hit in enumerate(hits, start=1):
            rid = hit.record_id
            slots = ranks.setdefault(rid, [absent] * len(legs))
            slots[leg] = min(slots[leg], rank)
            weight = 1.0 if weights is None or leg >= len(weights) else weights[leg]
            fused[rid] = fused.get(rid, 0.0) + weight / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], ranks[item[0]]))
