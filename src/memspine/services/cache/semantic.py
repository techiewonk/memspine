"""E3 semantic + operation caching: embedding (P1) + extraction (P3) caches.

Keys carry the *identity of the producer*, not just the content:

- embeddings — ``emb:{embedder_id}:{xxh64(content)}`` (model swap invalidates),
- extraction — ``ext:{prompt_version}:{xxh64(content)}`` (a prompt upgrade
  cleanly invalidates, D-43/N7 — prompt provenance as a cache key).
"""

from __future__ import annotations

import struct

import orjson
import xxhash

from memspine.prompts.models import ExtractedFact
from memspine.services.cache.base import KVCache
from memspine.services.embedding.base import EmbeddingService

__all__ = ["CachedEmbedding", "CachedExtractor"]


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


class CachedExtractor:
    """EntityExtractor decorator (E3): the same content extracted with the same
    prompt version is an LLM call saved; a prompt upgrade invalidates (N7)."""

    def __init__(self, inner: object, kv: KVCache) -> None:
        self._inner = inner
        self._kv = kv
        self.hits = 0
        self.misses = 0

    @property
    def prompt_version(self) -> str | None:
        version = getattr(self._inner, "prompt_version", None)
        return str(version) if version is not None else None

    def _key(self, content: str) -> str:
        return f"ext:{self.prompt_version}:{xxhash.xxh64_hexdigest(content.encode())}"

    async def extract(self, content: str) -> list[ExtractedFact]:
        key = self._key(content)
        cached = await self._kv.get(key)
        if cached is not None:
            self.hits += 1
            return [ExtractedFact.model_validate(item) for item in orjson.loads(cached)]
        self.misses += 1
        extract = getattr(self._inner, "extract")  # noqa: B009 - protocol member
        facts: list[ExtractedFact] = await extract(content)
        await self._kv.set(key, orjson.dumps([fact.model_dump() for fact in facts]))
        return facts
