"""Zero-age LanceDB version cleanup is safe for this store's own readers (RM-39).

The exclusive in-memory compaction (ADR-053) and the erasure purge (ADR-039) run
``optimize(cleanup_older_than=0)``, which deletes the data files of every older
table version. A read still on such a version fails with "Not found", so the
store gates reads against those two cleanups. With the gate, concurrent reads
never fail, the cleanup still leaves exactly one version, and LanceDB's
zero-age warning is not raised for these calls. A zero-age call made outside
the store still warns: the filter is scoped to the store's own call.
"""

from __future__ import annotations

import asyncio
import random
import warnings
from datetime import timedelta

import pytest

from memspine.clients.lancedb import LanceDBClient
from memspine.models.encoders.embedding.hash_local import HashEmbedding
from memspine.services.vector.lancedb_store import LanceDBVectorStore

_DIM = 16
_WRITES = 160
_READERS = 4


async def _store(path: str) -> LanceDBVectorStore:
    client = LanceDBClient(path)
    await client.connect()
    return LanceDBVectorStore(client, HashEmbedding(dim=_DIM), compact_every=4, exclusive=True)


async def test_concurrent_reads_survive_zero_age_cleanup() -> None:
    store = await _store("memory://cleanup")
    rnd = random.Random(5)
    vectors = [[rnd.uniform(-1, 1) for _ in range(_DIM)] for _ in range(_WRITES)]
    await store.upsert("seed", "ns", "hash", vectors[0])
    errors: list[BaseException] = []
    reads = 0
    finished = asyncio.Event()

    async def reader() -> None:
        nonlocal reads
        while not finished.is_set():
            try:
                await store.query("ns", vectors[rnd.randrange(_WRITES)], "hash", top_k=5)
                await store.exists("seed")
                reads += 2
            except Exception as exc:  # the regression under test
                errors.append(exc)

    async def writer() -> None:
        try:
            for i, vector in enumerate(vectors):
                await store.upsert(f"r{i}", "ns", "hash", vector)
                if i % 7 == 6:
                    await store.delete(f"r{i - 3}")
                if i % 25 == 24:
                    assert await store.purge_deleted()
        finally:
            finished.set()

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await asyncio.gather(writer(), *(reader() for _ in range(_READERS)))

    assert reads > 0
    assert errors == []
    assert not [w for w in caught if "cleanup_older_than=0" in str(w.message)]

    # Correctness is unchanged: deleted rows are gone from every retained
    # version, and the purge left only the latest version.
    await store.delete("r0")
    assert await store.purge_deleted()
    assert await store.history_absent("r0") is True
    assert not await store.exists("r0")
    assert await store.exists("r1")
    table = await store._ensure_table()
    assert len(table.list_versions()) == 1


async def test_a_zero_age_call_outside_the_store_still_warns() -> None:
    store = await _store("memory://foreign")
    await store.upsert("a", "ns", "hash", [1.0] + [0.0] * (_DIM - 1))
    table = await store._ensure_table()
    with pytest.warns(UserWarning, match=r"cleanup_older_than=0"):
        table.optimize(cleanup_older_than=timedelta(0))
