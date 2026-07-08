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
) -> list[tuple[str, float]]:
    """Reciprocal-rank fusion (D-25) of the vector and lexical legs.

    Each list contributes ``1 / (k + rank)`` per record (1-based rank in that
    list); a record present in both legs sums both contributions. Returns
    ``(record_id, fused_score)`` sorted by score descending, breaking ties by
    ``record_id`` ascending so the ranking is fully deterministic (two records
    that surface at identical ranks must not reorder run-to-run).
    """
    fused: dict[str, float] = {}
    for hits in (vector_hits, lexical_hits):
        for rank, hit in enumerate(hits, start=1):
            fused[hit.record_id] = fused.get(hit.record_id, 0.0) + 1.0 / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], item[0]))
