"""Hash embedder determinism + E3 cache behavior."""

from __future__ import annotations

from memspine.services.cache.base import MemoryKV
from memspine.services.cache.semantic import CachedEmbedding
from memspine.services.embedding.hash_local import HashEmbedding


async def test_hash_embedding_is_deterministic_and_normalized() -> None:
    embedder = HashEmbedding(dim=32)
    [a1] = await embedder.embed(["the sky is blue"])
    [a2] = await embedder.embed(["the sky is blue"])
    [b] = await embedder.embed(["completely different words entirely"])
    assert a1 == a2
    assert a1 != b
    assert abs(sum(x * x for x in a1) - 1.0) < 1e-6  # L2-normalized
    assert embedder.embedder_id == "hash:32"


async def test_shared_tokens_score_higher_than_disjoint() -> None:
    embedder = HashEmbedding(dim=64)
    [query], [near], [far] = (
        await embedder.embed(["blue sky today"]),
        await embedder.embed(["sky blue"]),
        await embedder.embed(["quantum flux capacitor"]),
    )

    def cos(u: list[float], v: list[float]) -> float:
        return sum(a * b for a, b in zip(u, v, strict=True))

    assert cos(query, near) > cos(query, far)


async def test_cached_embedding_hits_after_first_call() -> None:
    inner = HashEmbedding(dim=16)
    cached = CachedEmbedding(inner, MemoryKV())

    first = await cached.embed(["alpha", "beta"])
    assert (cached.hits, cached.misses) == (0, 2)

    second = await cached.embed(["alpha", "beta", "gamma"])
    assert cached.hits == 2 and cached.misses == 3
    assert second[:2] == first  # cache returns identical vectors
    assert cached.embedder_id == inner.embedder_id  # key identity preserved (E3)


async def test_cache_mixes_hits_and_misses_in_order() -> None:
    cached = CachedEmbedding(HashEmbedding(dim=16), MemoryKV())
    await cached.embed(["b"])
    vectors = await cached.embed(["a", "b", "c"])
    direct = await HashEmbedding(dim=16).embed(["a", "b", "c"])
    assert vectors == direct  # order preserved regardless of hit/miss layout


async def test_duplicate_texts_in_one_batch_embed_once() -> None:
    """Regression: same-batch duplicates used to hit the inner embedder N times."""

    calls: list[list[str]] = []

    class CountingEmbedder(HashEmbedding):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            calls.append(texts)
            return await super().embed(texts)

    inner = CountingEmbedder(dim=16)
    cached = CachedEmbedding(inner, MemoryKV())
    vectors = await cached.embed(["dup", "unique", "dup", "dup"])
    assert calls == [["dup", "unique"]]  # each unique text embedded once
    assert vectors[0] == vectors[2] == vectors[3]
    direct = await HashEmbedding(dim=16).embed(["dup", "unique", "dup", "dup"])
    assert vectors == direct


async def test_memory_kv_ttl_and_eviction() -> None:
    kv = MemoryKV(max_entries=2)
    await kv.set("a", b"1")
    await kv.set("b", b"2")
    await kv.set("c", b"3")  # cap reached: oldest-inserted ('a') dropped
    assert await kv.get("a") is None
    assert await kv.get("b") == b"2" and await kv.get("c") == b"3"

    await kv.set("t", b"x", ttl_seconds=0.0)  # already expired
    assert await kv.get("t") is None
    await kv.delete("b")
    assert await kv.get("b") is None
