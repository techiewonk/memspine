"""E1: configurable graph-traversal strategies for related() (amends D-49).

ppr (default) stays PageRank; bfs walks neighbors by hop; rrf fuses the graph
rank with a vector-similarity rank and degrades to ppr without a vector store.
Uses the shared associative harness (real SQLite-adjacency graph + projector).
"""

from __future__ import annotations

from assoc_helpers import EventLog, FakeStorage

from memspine.exceptions import ConfigError
from memspine.memories.associative.store import AssociativeMemory
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.vector.base import VectorHit


class FakeEmbedder:
    embedder_id = "fake:8"

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[float(len(text)), 1.0] for text in texts]


class FakeVector:
    """Returns a fixed similarity ranking regardless of the query vector."""

    def __init__(self, ranking: list[str]) -> None:
        self._ranking = ranking

    async def query(self, namespace, vector, embedder_id, top_k=8):  # type: ignore[no-untyped-def]
        return [VectorHit(rid, 1.0 - i * 0.1) for i, rid in enumerate(self._ranking)][:top_k]


def _mem(storage, graph, log, **kw):  # type: ignore[no-untyped-def]
    return AssociativeMemory(storage, graph, log.append, **kw)


def _record(content: str):  # type: ignore[no-untyped-def]
    from memspine.core.records import MemoryRecord

    return MemoryRecord(namespace="default", memory_type="semantic", content=content)


async def _chain(storage: FakeStorage, memory: AssociativeMemory, n: int) -> list[str]:
    """An A-B-C-... linked chain; returns the record ids in order."""
    ids: list[str] = []
    prev: str | None = None
    for i in range(n):
        rec = storage.add(_record(chr(ord("a") + i)))
        ids.append(rec.record_id)
        if prev is not None:
            await memory.link("default", prev, rec.record_id, weight=1.0)
        prev = rec.record_id
    return ids


async def test_ppr_is_the_default_strategy(
    storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    memory = _mem(storage, graph, log)
    ids = await _chain(storage, memory, 3)  # a-b-c
    related = await memory.related("default", ids[0])
    got = {r.record_id for r in related}
    assert ids[1] in got and ids[2] in got  # b and c reachable, a (seed) excluded
    assert ids[0] not in got


async def test_bfs_strategy_walks_by_hops(
    storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    memory = _mem(storage, graph, log, policies={"related": {"strategy": "bfs", "depth": 1}})
    ids = await _chain(storage, memory, 3)  # a-b-c
    # depth=1: only the direct neighbor b, not c.
    related = await memory.related("default", ids[0])
    assert {r.record_id for r in related} == {ids[1]}


async def test_rrf_fuses_graph_and_vector_ranks(
    storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    ids_holder: list[str] = []
    memory = _mem(
        storage,
        graph,
        log,
        policies={"related": {"strategy": "rrf"}},
        vector=FakeVector(ids_holder),  # filled after the chain exists
        embedder=FakeEmbedder(),
    )
    ids = await _chain(storage, memory, 3)  # a-b-c
    ids_holder.extend([ids[2], ids[1]])  # vector ranks c above b
    related = await memory.related("default", ids[0])
    got = {r.record_id for r in related}
    assert got == {ids[1], ids[2]}  # both surface; seed excluded


async def test_rrf_falls_back_to_ppr_without_vector(
    storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    memory = _mem(storage, graph, log, policies={"related": {"strategy": "rrf"}})
    ids = await _chain(storage, memory, 3)
    related = await memory.related("default", ids[0])  # no vector => ppr
    assert related  # did not crash; returned graph-related records
    assert ids[0] not in {r.record_id for r in related}


async def test_call_arg_overrides_policy_strategy(
    storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    memory = _mem(storage, graph, log, policies={"related": {"strategy": "ppr"}})
    ids = await _chain(storage, memory, 3)
    related = await memory.related("default", ids[0], strategy="bfs")
    # bfs default depth=2 reaches both b and c on the a-b-c chain.
    assert {r.record_id for r in related} == {ids[1], ids[2]}


async def test_unknown_strategy_is_a_config_error(
    storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    memory = _mem(storage, graph, log)
    ids = await _chain(storage, memory, 2)
    import pytest

    with pytest.raises(ConfigError, match="unknown related strategy"):
        await memory.related("default", ids[0], strategy="astar")
