"""Deterministic hash embedder: zero-network, zero-model provider.

For tests, CI, and air-gapped smoke runs (config: ``embedding.provider: hash``).
Vectors are stable across runs (xxh64-seeded token buckets, L2-normalized) and
similar texts land near each other only insofar as they share tokens — good
enough to exercise the retrieval plumbing, useless for semantics. Never the
production default (that is fastembed, D-08).
"""

from __future__ import annotations

import math

import xxhash

from memspine.config.constants import HASH_EMBEDDING_DIM

__all__ = ["HashEmbedding"]


class HashEmbedding:
    def __init__(self, dim: int = HASH_EMBEDDING_DIM) -> None:
        self._dim = dim

    @property
    def embedder_id(self) -> str:
        return f"hash:{self._dim}"

    @property
    def dim(self) -> int:
        return self._dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]

    def _embed_one(self, text: str) -> list[float]:
        vector = [0.0] * self._dim
        for token in text.lower().split():
            digest = xxhash.xxh64_intdigest(token)
            bucket = digest % self._dim
            sign = 1.0 if (digest >> 63) & 1 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(component * component for component in vector))
        if norm == 0.0:
            vector[0] = 1.0
            return vector
        return [component / norm for component in vector]
