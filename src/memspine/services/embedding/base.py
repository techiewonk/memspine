"""Embedding port. ``embedder_id`` participates in E3 cache keys and vector
store metadata so a model swap cleanly invalidates both.

The manifest (E4, ADR-020) declares storage/speed capabilities the vector store
exploits: ``matryoshka_dims`` (truncatable prefix dimensions) and
``quantization`` (``"int8"``/``"binary"``; ``None`` = float32). The zero-dep
SQLite store reads these to drive its two-stage ``search_rescore``. The core
default embedders (hash, fastembed) declare neither — an unclaimed capability
is safer than a guessed one (D-10) — so the default retrieval path is unchanged;
an embedder that declares one, or a ``vector.quantization`` config override,
opts in.
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
