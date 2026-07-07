"""E3 semantic + operation caching: the embedding cache lands in Phase 1.

Key = ``emb:{embedder_id}:{xxh64(content)}`` — embedder identity is part of the
key so a model swap cleanly invalidates (extraction/judge caches join in P3 and
add prompt-version to their keys, D-43).
"""

from __future__ import annotations

import struct

import xxhash

from memspine.services.cache.base import KVCache
from memspine.services.embedding.base import EmbeddingService

__all__ = ["CachedEmbedding"]


def _pack(vector: list[float]) -> bytes:
    return struct.pack(f"<{len(vector)}f", *vector)


def _unpack(raw: bytes) -> list[float]:
    count = len(raw) // 4
    return list(struct.unpack(f"<{count}f", raw))


class CachedEmbedding:
    """EmbeddingService decorator: content-hash cache in front of any embedder (E3)."""

    def __init__(self, inner: EmbeddingService, kv: KVCache) -> None:
        self._inner = inner
        self._kv = kv
        self.hits = 0
        self.misses = 0

    @property
    def embedder_id(self) -> str:
        return self._inner.embedder_id

    @property
    def dim(self) -> int:
        return self._inner.dim

    def _key(self, text: str) -> str:
        return f"emb:{self._inner.embedder_id}:{xxhash.xxh64_hexdigest(text.encode())}"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        vectors: list[list[float] | None] = []
        missing: list[str] = []
        missing_at: list[int] = []
        for index, text in enumerate(texts):
            cached = await self._kv.get(self._key(text))
            if cached is not None:
                self.hits += 1
                vectors.append(_unpack(cached))
            else:
                self.misses += 1
                vectors.append(None)
                missing.append(text)
                missing_at.append(index)
        if missing:
            fresh = await self._inner.embed(missing)
            for position, vector in zip(missing_at, fresh, strict=True):
                vectors[position] = vector
                await self._kv.set(self._key(texts[position]), _pack(vector))
        return [vector for vector in vectors if vector is not None]
