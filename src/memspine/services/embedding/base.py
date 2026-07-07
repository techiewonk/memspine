"""Embedding port. ``embedder_id`` participates in E3 cache keys and vector
store metadata so a model swap cleanly invalidates both."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

__all__ = ["EmbeddingService"]


@runtime_checkable
class EmbeddingService(Protocol):
    @property
    def embedder_id(self) -> str:
        """Stable identity: ``provider:model`` — part of cache keys (E3)."""
        ...

    @property
    def dim(self) -> int: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...
