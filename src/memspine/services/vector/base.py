"""VectorStore port (D-09): upsert / query / delete-by-record.

Vector stores are projections (D0.1): rebuildable from the event log by
re-embedding. ``search_rescore()`` is the E4 two-stage retrieval (ADR-020):
a quantized (int8/binary) or Matryoshka-truncated prefilter over an oversampled
candidate window, then an exact float32 cosine rescore. The zero-dep SQLite
store implements it in pure Python; adapters that own native quantization (or
have nothing to quantize) degenerate it to ``query`` — byte-identical.
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
        """E4 two-stage retrieval (ADR-020): a quantized/Matryoshka-truncated
        prefilter over an oversampled candidate window → exact float32 cosine
        rescore → ``top_k``. Active only when the store was constructed with a
        quantization/truncation config (driven by the embedder manifest +
        ``vector.quantization`` override); otherwise it is exactly ``query`` —
        the guard that keeps ``profile="simple"`` byte-identical."""
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
