"""VectorStore port (D-09): upsert / query / delete-by-record.

Vector stores are projections (D0.1): rebuildable from the event log by
re-embedding. ``search_rescore()`` (E4 quantization rescore) joins in Phase 6.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["VectorHit", "VectorStore"]


@dataclass(frozen=True)
class VectorHit:
    record_id: str
    score: float  # cosine similarity in [-1, 1]


@runtime_checkable
class VectorStore(Protocol):
    async def upsert(
        self, record_id: str, namespace: str, embedder_id: str, vector: list[float]
    ) -> None: ...

    async def query(
        self, namespace: str, vector: list[float], top_k: int = 8
    ) -> list[VectorHit]: ...

    async def delete(self, record_id: str) -> None: ...

    async def delete_all(self) -> None:
        """Projector rebuild support."""
        ...
