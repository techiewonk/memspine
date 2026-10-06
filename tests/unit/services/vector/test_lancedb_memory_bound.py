"""An in-memory LanceDB table's committed memory stays bounded (ADR-053).

A ``memory://`` store keeps every table version's files, each holding a 5 MB
upload buffer, so an exclusive store that never dropped old versions committed
~15 MB per write. The test reads the process's private (committed) bytes, which
``psutil`` reports on Windows only; resident memory does not show the leak.
"""

from __future__ import annotations

import random

import pytest

pytest.importorskip("lancedb")
pytest.importorskip("psutil")

import psutil

from memspine.clients.lancedb import LanceDBClient
from memspine.config import constants
from memspine.services.embedding.hash_local import HashEmbedding
from memspine.services.vector.lancedb_store import LanceDBVectorStore

_DIM = 16
_WRITES = 2000
# Unfixed, 2000 writes commit ~30 GB; fixed, at most one compaction window of
# files (~20 writes x 15 MB) is alive at once.
_BOUND_MB = 800.0


def _private_mb() -> float:
    info = psutil.Process().memory_info()
    return float(info.private) / 2**20


@pytest.mark.skipif(
    not hasattr(psutil.Process().memory_info(), "private"),
    reason="psutil reports private memory on Windows only",
)
async def test_in_memory_store_private_memory_stays_bounded() -> None:
    client = LanceDBClient("memory://vectors")
    await client.connect()
    store = LanceDBVectorStore(
        client,
        HashEmbedding(dim=_DIM),
        compact_every=constants.LANCE_COMPACT_EVERY,
        exclusive=True,
    )
    rnd = random.Random(7)
    vectors = [[rnd.uniform(-1, 1) for _ in range(_DIM)] for _ in range(_WRITES)]
    await store.upsert("warm", "ns", "hash", vectors[0])
    base = _private_mb()
    for i, vector in enumerate(vectors):
        await store.upsert(f"r{i}", "ns", "hash", vector)
        if i % 50 == 49:
            await store.delete(f"r{i - 1}")
        if i % 20 == 0:
            # Check as it grows, so a regression fails fast instead of
            # committing gigabytes.
            assert _private_mb() - base < _BOUND_MB, f"after {i + 1} writes"
    assert _private_mb() - base < _BOUND_MB
    hits = await store.query("ns", vectors[-1], "hash", top_k=1)
    assert [hit.record_id for hit in hits] == [f"r{_WRITES - 1}"]
    await client.close()
