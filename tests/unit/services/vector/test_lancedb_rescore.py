"""E4 native rescore for the LanceDB store (ADR-020 §6).

LanceDB realizes the two-stage "quantized prefilter → exact rescore" through a
native compressed ANN index (IVF_HNSW_SQ for int8, IVF_PQ otherwise) queried with
``nprobes`` + ``refine_factor`` — not the SQLite store's pure-Python codes. These
tests assert: (1) with quantization active over a corpus large enough to train an
index, the quantized rescore returns the SAME top-1 as an exact flat scan on a
controlled set (the ~95%-recall claim, 100% on a well-separated set); (2) the
inactive path (quantization=None) is byte-identical to query(); (3) below the
row threshold the store degrades to a flat exact query and builds no index.
"""

from __future__ import annotations

import math
import random
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("lancedb")

from memspine.clients.lancedb import LanceDBClient
from memspine.config import constants
from memspine.services.embedding.hash_local import HashEmbedding
from memspine.services.vector.lancedb_store import LanceDBVectorStore

_DIM = 64


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(c * c for c in vector))
    return [c / norm for c in vector] if norm else vector


async def _make_store(tmp_path: Path, **kwargs: Any) -> LanceDBVectorStore:
    client = LanceDBClient(tmp_path / "vec.lance")
    await client.connect()
    return LanceDBVectorStore(client, HashEmbedding(dim=_DIM), **kwargs)


def _exact_top1(query: list[float], corpus: dict[str, list[float]]) -> str:
    """Pure-Python exact argmax cosine — the ground-truth reference the native
    quantized rescore must match (independent of any Lance index/query path)."""
    return max(corpus, key=lambda rid: sum(a * b for a, b in zip(query, corpus[rid], strict=True)))


async def test_inactive_rescore_is_byte_identical_to_query(tmp_path: Path) -> None:
    """quantization=None (default embedders): search_rescore must be exactly
    query() — no index requirement, plain flat cosine, profile='simple' guard."""
    store = await _make_store(tmp_path)  # no quantization → inactive
    assert store._rescore_active is False
    rng = random.Random(1)
    for i in range(20):
        vec = _normalize([rng.gauss(0, 1) for _ in range(_DIM)])
        await store.upsert(str(i), "ns", store._embedder.embedder_id, vec)
    query = _normalize([rng.gauss(0, 1) for _ in range(_DIM)])
    eid = store._embedder.embedder_id
    rescored = await store.search_rescore("ns", query, eid, top_k=5)
    plain = await store.query("ns", query, eid, top_k=5)
    assert rescored == plain  # identical objects/order — the byte-identical path
    # And no ANN index was ever created on the inactive path.
    table = await store._ensure_table()
    assert table.list_indices() == []


async def test_int8_rescore_matches_exact_top1(tmp_path: Path) -> None:
    """int8 manifest over a corpus past the training threshold: an IVF_HNSW_SQ
    index is built and the native rescore returns the exact-flat top-1."""
    store = await _make_store(tmp_path, quantization="int8")
    assert store._rescore_active is True
    rng = random.Random(7)
    n = constants.LANCE_ANN_MIN_ROWS + 44  # 300, comfortably past 256
    centers = {str(i): _normalize([rng.gauss(0, 1) for _ in range(_DIM)]) for i in range(n)}
    eid = store._embedder.embedder_id
    for rid, vec in centers.items():
        await store.upsert(rid, "ns", eid, vec)

    matches = 0
    trials = 60
    for _ in range(trials):
        rid = str(rng.randrange(n))
        # A jittered copy of a stored point → an unambiguous, well-separated top-1.
        query = _normalize([c + rng.gauss(0, 0.01) for c in centers[rid]])
        hits = await store.search_rescore("ns", query, eid, top_k=1)
        assert hits, "active rescore must return candidates once the index exists"
        if hits[0].record_id == _exact_top1(query, centers):
            matches += 1

    # A native compressed ANN index was actually built (not a flat fallback).
    assert store._index_ready is True
    table = await store._ensure_table()
    assert any("vector" in idx.columns for idx in table.list_indices())
    # ~95% recall claim — 100% on this controlled well-separated set.
    assert matches == trials


async def test_binary_builds_index_and_returns_hits(tmp_path: Path) -> None:
    """binary manifest → IVF_PQ index; rescore returns the exact-flat top-1."""
    store = await _make_store(tmp_path, quantization="binary")
    rng = random.Random(11)
    n = constants.LANCE_ANN_MIN_ROWS + 44
    centers = {str(i): _normalize([rng.gauss(0, 1) for _ in range(_DIM)]) for i in range(n)}
    eid = store._embedder.embedder_id
    for rid, vec in centers.items():
        await store.upsert(rid, "ns", eid, vec)

    rid = str(rng.randrange(n))
    query = _normalize([c + rng.gauss(0, 0.01) for c in centers[rid]])
    hits = await store.search_rescore("ns", query, eid, top_k=1)
    assert store._index_ready is True
    assert hits and hits[0].record_id == _exact_top1(query, centers)


async def test_below_threshold_falls_back_to_flat(tmp_path: Path) -> None:
    """Too few rows to train an index: the active store degrades to a flat exact
    query (skip-logged once), builds no index, and stays correct."""
    store = await _make_store(tmp_path, quantization="int8")
    rng = random.Random(3)
    n = 40  # well under LANCE_ANN_MIN_ROWS (256)
    corpus = {str(i): _normalize([rng.gauss(0, 1) for _ in range(_DIM)]) for i in range(n)}
    eid = store._embedder.embedder_id
    for rid, vec in corpus.items():
        await store.upsert(rid, "ns", eid, vec)

    query = _normalize([rng.gauss(0, 1) for _ in range(_DIM)])
    hits = await store.search_rescore("ns", query, eid, top_k=1)
    assert hits and hits[0].record_id == _exact_top1(query, corpus)
    # No index trained; the deferral was noted and the ANN path is not "ready".
    assert store._index_ready is False
    assert store._deferred_logged is True
    table = await store._ensure_table()
    assert table.list_indices() == []
