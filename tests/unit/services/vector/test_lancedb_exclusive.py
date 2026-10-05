"""In-memory LanceDB tables: the exclusive fast path and periodic compaction.

An exclusive store appends unseen ids instead of merging, and an in-memory
store compacts its fragments every few upserts. Neither may change what a
query returns compared with the plain merge-per-upsert store, including the
order of exact ties (identical vectors), which LanceDB breaks by row order.
"""

from __future__ import annotations

import random
from typing import Any

import pytest

pytest.importorskip("lancedb")

from memspine.clients.lancedb import LanceDBClient
from memspine.services.embedding.hash_local import HashEmbedding
from memspine.services.vector.base import VectorHit
from memspine.services.vector.lancedb_store import LanceDBVectorStore

_DIM = 16


async def _store(**kwargs: Any) -> LanceDBVectorStore:
    client = LanceDBClient("memory://vectors")
    await client.connect()
    return LanceDBVectorStore(client, HashEmbedding(dim=_DIM), **kwargs)


def _vectors() -> list[list[float]]:
    rnd = random.Random(3)
    distinct = [[rnd.uniform(-1, 1) for _ in range(_DIM)] for _ in range(9)]
    return [distinct[i % 9] for i in range(45)]  # five copies of each: exact ties


async def _replay(store: LanceDBVectorStore) -> list[list[VectorHit]]:
    vectors = _vectors()
    for i, vector in enumerate(vectors):
        await store.upsert(f"r{i}", "ns", "hash", vector)
        if i % 10 == 9:
            # Re-upsert an existing id: it must replace, never duplicate.
            await store.upsert(f"r{i - 5}", "ns", "hash", vectors[(i + 1) % 9])
    await store.delete("r3")
    await store.upsert("r3", "ns", "hash", vectors[3])
    return [await store.query("ns", vector, "hash", top_k=12) for vector in vectors[:9]]


async def test_exclusive_and_compacted_store_matches_the_plain_store() -> None:
    plain = await _replay(await _store())
    fast = await _replay(await _store(compact_every=4, exclusive=True))
    assert fast == plain


async def test_exclusive_store_replaces_an_existing_id() -> None:
    store = await _store(exclusive=True)
    await store.upsert("a", "ns", "hash", [1.0] + [0.0] * (_DIM - 1))
    await store.upsert("a", "ns", "hash", [0.0, 1.0] + [0.0] * (_DIM - 2))
    hits = await store.query("ns", [0.0, 1.0] + [0.0] * (_DIM - 2), "hash", top_k=5)
    assert [hit.record_id for hit in hits] == ["a"]
    assert hits[0].score == pytest.approx(1.0)
    await store.delete_all()
    assert not await store.exists("a")
    await store.upsert("a", "ns", "hash", [1.0] + [0.0] * (_DIM - 1))
    assert await store.exists("a")
