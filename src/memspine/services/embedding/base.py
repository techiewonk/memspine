"""Embedding port. ``embedder_id`` participates in E3 cache keys and vector
store metadata so a model swap cleanly invalidates both.

The manifest (E4, plan Phase 6) declares storage/speed capabilities:
``matryoshka_dims`` (truncatable prefix dimensions) and ``quantization``
(``"int8"``/``"binary"``; ``None`` = float32). Both are seams in v0.1 — the
core defaults declare no capability; quantized adapters fill them in later.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

__all__ = ["EmbedderManifest", "EmbeddingService"]


@dataclass(frozen=True)
class EmbedderManifest:
    """What a vector store may exploit about this embedder (E4)."""

    embedder_id: str
    dim: int
    #: Matryoshka prefix dims usable for truncated prefilter search, or None.
    matryoshka_dims: tuple[int, ...] | None = None
    #: Quantization the emitted vectors tolerate ("int8" | "binary"), or None.
    quantization: str | None = None


@runtime_checkable
class EmbeddingService(Protocol):
    @property
    def embedder_id(self) -> str:
        """Stable identity: ``provider:model`` — part of cache keys (E3)."""
        ...

    @property
    def dim(self) -> int: ...

    @property
    def manifest(self) -> EmbedderManifest:
        """E4 capability declaration; storage seams read, never guess."""
        ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
