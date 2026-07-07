"""SQLite adjacency fallback: protocol conformance + rebuildable-projection contract."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.services.graph.base import GraphStore
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.storage.sqlite.schema import metadata


@pytest.fixture
async def store() -> AsyncIterator[SQLiteAdjacencyGraph]:
    client = SQLiteClient(":memory:")
    await client.connect()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield SQLiteAdjacencyGraph(client)
    await client.close()


async def test_satisfies_graph_store_protocol(store: SQLiteAdjacencyGraph) -> None:
    assert isinstance(store, GraphStore)


async def test_upsert_node_is_idempotent_and_replaces(store: SQLiteAdjacencyGraph) -> None:
    await store.upsert_node("m1", labels=["memory"], properties={"kind": "fact"})
    await store.upsert_node("m1", labels=["memory", "hot"], properties={"kind": "skill"})
    assert await store.node_count() == 1

    await store.upsert_edge("m0", "m1", "related")
    [node] = await store.neighbors("m0")
    assert node.node_id == "m1"
    assert node.labels == ("memory", "hot")  # second upsert fully replaced
    assert node.properties == {"kind": "skill"}


async def test_upsert_edge_creates_bare_endpoints_and_is_idempotent(
    store: SQLiteAdjacencyGraph,
) -> None:
    await store.upsert_edge("a", "b", "related", {"weight": 0.5})
    await store.upsert_edge("a", "b", "related", {"weight": 0.9})  # replace, not duplicate
    assert await store.node_count() == 2  # endpoints implicitly created
    assert await store.edge_count() == 1

    [edge] = await store.edges_of("a")
    assert (edge.src, edge.dst, edge.rel_type) == ("a", "b", "related")
    assert edge.weight == 0.9


async def test_edge_weight_defaults_to_one_when_absent_or_non_numeric(
    store: SQLiteAdjacencyGraph,
) -> None:
    await store.upsert_edge("a", "b", "related")
    await store.upsert_edge("a", "c", "related", {"weight": "heavy"})
    weights = {edge.dst: edge.weight for edge in await store.edges_of("a")}
    assert weights == {"b": 1.0, "c": 1.0}


async def test_neighbors_filters_rel_type_and_traverses_undirected(
    store: SQLiteAdjacencyGraph,
) -> None:
    await store.upsert_edge("a", "b", "related")
    await store.upsert_edge("c", "a", "derived_from")  # incoming edge still a neighbour

    assert {n.node_id for n in await store.neighbors("a")} == {"b", "c"}
    assert {n.node_id for n in await store.neighbors("a", rel_type="related")} == {"b"}
    assert {n.node_id for n in await store.neighbors("a", rel_type="derived_from")} == {"c"}
    assert await store.neighbors("a", rel_type="nope") == []


async def test_neighbors_depth_walk_excludes_start_and_stops_at_depth(
    store: SQLiteAdjacencyGraph,
) -> None:
    # chain a - b - c - d
    await store.upsert_edge("a", "b", "related")
    await store.upsert_edge("b", "c", "related")
    await store.upsert_edge("c", "d", "related")

    assert {n.node_id for n in await store.neighbors("a", depth=1)} == {"b"}
    assert {n.node_id for n in await store.neighbors("a", depth=2)} == {"b", "c"}
    assert {n.node_id for n in await store.neighbors("a", depth=3)} == {"b", "c", "d"}
    # a cycle must not re-yield the start node or loop forever
    await store.upsert_edge("d", "a", "related")
    assert {n.node_id for n in await store.neighbors("a", depth=10)} == {"b", "c", "d"}
    assert await store.neighbors("a", depth=0) == []


async def test_neighbors_treat_tombstoned_edges_as_gone(store: SQLiteAdjacencyGraph) -> None:
    """Every reader treats weight <= 0 as gone (ADR-015): a pruned neighbour
    must not resurface via neighbors()/walks (M2, P6 review)."""
    await store.upsert_edge("a", "b", "related", {"weight": 0.5})
    await store.upsert_edge("b", "c", "related", {"weight": 0.5})
    assert {n.node_id for n in await store.neighbors("a", depth=2)} == {"b", "c"}

    await store.upsert_edge("a", "b", "related", {"weight": 0.0})  # prune tombstone
    assert await store.neighbors("a") == []
    assert await store.neighbors("a", depth=5) == []  # walk blocked at the tombstone
    # ...in both directions, and c-side reach through b is untouched.
    assert {n.node_id for n in await store.neighbors("b")} == {"c"}
    assert {n.node_id for n in await store.neighbors("c", depth=2)} == {"b"}


async def test_delete_node_cascades_edges_both_directions(store: SQLiteAdjacencyGraph) -> None:
    await store.upsert_edge("a", "b", "related")
    await store.upsert_edge("c", "b", "related")
    await store.upsert_edge("b", "d", "related")

    await store.delete_node("b")  # M7 forget cascade
    assert await store.node_count() == 3  # a, c, d remain
    assert await store.edge_count() == 0
    assert await store.edges_of("a") == []
    assert await store.neighbors("a") == []
    # deleting a node that is already gone is idempotent (replay safety)
    await store.delete_node("b")


async def test_edge_list_exports_every_edge_for_community_detection(
    store: SQLiteAdjacencyGraph,
) -> None:
    await store.upsert_edge("a", "b", "related", {"weight": 0.7})
    await store.upsert_edge("b", "c", "derived_from")
    exported = {(e.src, e.dst, e.rel_type, e.weight) for e in await store.edge_list()}
    assert exported == {("a", "b", "related", 0.7), ("b", "c", "derived_from", 1.0)}


async def test_clear_then_replay_reproduces_identical_projection(
    store: SQLiteAdjacencyGraph,
) -> None:
    """The rebuildable-projection contract (D0.1): clear() + replaying the same
    upserts yields the same graph, byte for byte."""

    async def replay(target: SQLiteAdjacencyGraph) -> None:
        await target.upsert_node("m1", labels=["memory"], properties={"kind": "fact"})
        await target.upsert_edge("m1", "m2", "related", {"weight": 0.8})
        await target.upsert_edge("m2", "m3", "derived_from")

    await replay(store)
    before_edges = sorted(
        (e.src, e.dst, e.rel_type, tuple(sorted(e.properties.items())))
        for e in await store.edge_list()
    )
    before_counts = (await store.node_count(), await store.edge_count())

    await store.clear()
    assert (await store.node_count(), await store.edge_count()) == (0, 0)
    assert await store.edge_list() == []

    await replay(store)
    after_edges = sorted(
        (e.src, e.dst, e.rel_type, tuple(sorted(e.properties.items())))
        for e in await store.edge_list()
    )
    assert after_edges == before_edges
    assert (await store.node_count(), await store.edge_count()) == before_counts
