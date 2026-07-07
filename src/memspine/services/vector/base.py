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
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        """Rank by cosine within a namespace. ``embedder_id`` scopes the search:
        rows written by a different embedder (model swap without rebuild) must
        be excluded, never silently compared across dimensions."""
        ...

    async def search_rescore(
        self, namespace: str, vector: list[float], embedder_id: str, top_k: int = 8
    ) -> list[VectorHit]:
        """E4 two-stage seam (plan Phase 6): quantized/truncated prefilter →
        full-precision rescore. v0.1 adapters implement this as a fallback to
        plain ``query`` — the real prefilter is adapter work gated on an
        embedder manifest that declares ``quantization``/``matryoshka_dims``."""
        ...

    async def delete(self, record_id: str) -> None: ...

    async def delete_all(self) -> None:
        """Projector rebuild support."""
        ...

    async def exists(self, record_id: str) -> bool:
        """M7 erasure proof: is a row for this record still present? Backends
        that cannot answer honestly should not implement this — the engine
        treats an absent capability as *unproven*, never as clean."""
        ...
