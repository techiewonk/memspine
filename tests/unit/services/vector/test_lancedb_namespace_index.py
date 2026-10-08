"""I1: the LanceDB ``namespace`` BITMAP index (vector.namespace_index, off by default)."""

from __future__ import annotations

import math
import random
from pathlib import Path

import pytest

pytest.importorskip("lancedb")

from memspine.clients.lancedb import LanceDBClient
from memspine.config import constants
from memspine.config.schema import VectorConfig
from memspine.services.embedding.hash_local import HashEmbedding
from memspine.services.vector.lancedb_store import LanceDBVectorStore

_DIM = 16


def _unit(v: list[float]) -> list[float]:
    n = math.sqrt(sum(x * x for x in v))
    return [x / n for x in v]


async def _store(tmp_path: Path, on: bool) -> LanceDBVectorStore:
    client = LanceDBClient(tmp_path / ("on" if on else "off") / "vec.lance")
    await client.connect()
    return LanceDBVectorStore(client, HashEmbedding(dim=_DIM), namespace_index=on)


def test_off_by_default() -> None:
    assert VectorConfig().namespace_index is False


async def test_index_is_built_and_results_are_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(constants, "NAMESPACE_INDEX_EVERY", 5)
    monkeypatch.setattr(constants, "NAMESPACE_INDEX_MIN_ROWS", 10)
    with_index = await _store(tmp_path, True)
    without = await _store(tmp_path, False)
    rng = random.Random(7)
    eid = with_index._embedder.embedder_id
    for i in range(30):
        vec = _unit([rng.gauss(0, 1) for _ in range(_DIM)])
        ns = "alice" if i % 2 else "bob"
        for store in (with_index, without):
            await store.upsert(f"r{i}", ns, eid, vec)
    table = await with_index._ensure_table()
    names = [str(getattr(ix, "columns", ix)) for ix in table.list_indices()]
    assert any("namespace" in n for n in names)
    query = _unit([rng.gauss(0, 1) for _ in range(_DIM)])
    for ns in ("alice", "bob"):
        a = await with_index.query(ns, query, eid, top_k=10)
        b = await without.query(ns, query, eid, top_k=10)
        assert [h.record_id for h in a] == [h.record_id for h in b]
        assert all(int(h.record_id[1:]) % 2 == (1 if ns == "alice" else 0) for h in a)
