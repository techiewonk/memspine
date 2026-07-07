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
        # Duplicate texts in one batch embed once: positions grouped per key.
        miss_positions: dict[str, list[int]] = {}
        miss_texts: dict[str, str] = {}
        for index, text in enumerate(texts):
            key = self._key(text)
            if key in miss_positions:
                self.hits += 1  # same-batch duplicate: served by the first miss
                vectors.append(None)
                miss_positions[key].append(index)
                continue
            cached = await self._kv.get(key)
            if cached is not None:
                self.hits += 1
                vectors.append(_unpack(cached))
            else:
                self.misses += 1
                vectors.append(None)
                miss_positions[key] = [index]
                miss_texts[key] = text
        if miss_texts:
            keys = list(miss_texts)
            fresh = await self._inner.embed([miss_texts[key] for key in keys])
            for key, vector in zip(keys, fresh, strict=True):
                for position in miss_positions[key]:
                    vectors[position] = vector
                await self._kv.set(key, _pack(vector))
        return [vector for vector in vectors if vector is not None]
