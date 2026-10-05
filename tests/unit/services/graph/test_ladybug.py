"""LadybugGraphStore adapter coverage (D-26, ``[graph]``).

Mirrors what ``test_parity.py`` exercises against every adapter, plus the
ladybug-specific cases the shared parity test doesn't cover (delete cascade,
clear, close). Skipped whole-file when ``ladybug`` is not installed.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from memspine.services.graph.base import GraphStore

pytest.importorskip("ladybug")

from memspine.clients.ladybug import LadybugClient
from memspine.services.graph.ladybug import LadybugGraphStore


@pytest.fixture
async def store() -> AsyncIterator[GraphStore]:
    client = LadybugClient(":memory:")
    await client.connect()
    yield LadybugGraphStore(client)
    await client.close()


async def test_upsert_node_and_edge(store: GraphStore) -> None:
    await store.upsert_node("m1", labels=["memory"], properties={"kind": "fact"})
    await store.upsert_edge("m1", "m2", "related", {"weight": 0.8})
    assert await store.node_count() == 2
    assert await store.edge_count() == 1


async def test_upsert_edge_implicitly_creates_bare_endpoints(store: GraphStore) -> None:
    """Link replay must never depend on node-event ordering (port contract)."""
    await store.upsert_edge("a", "b", "related")
    assert await store.node_count() == 2
    nodes = await store.neighbors("a")
    assert [n.node_id for n in nodes] == ["b"]
    assert nodes[0].labels == ()
    assert nodes[0].properties == {}


async def test_upsert_edge_is_idempotent_replace(store: GraphStore) -> None:
    await store.upsert_edge("a", "b", "related", {"weight": 0.5})
    await store.upsert_edge("a", "b", "related", {"weight": 0.9})
    assert await store.edge_count() == 1
    (edge,) = await store.edge_list()
    assert edge.weight == 0.9


async def test_neighbors_respects_rel_type_and_depth(store: GraphStore) -> None:
    await store.upsert_edge("a", "b", "related")
    await store.upsert_edge("b", "c", "derived_from")
    assert {n.node_id for n in await store.neighbors("a", rel_type="related")} == {"b"}
    assert {n.node_id for n in await store.neighbors("a", rel_type="derived_from")} == set()
    assert {n.node_id for n in await store.neighbors("a", depth=2)} == {"b", "c"}


async def test_neighbors_filters_tombstoned_edges_but_edge_list_does_not(
    store: GraphStore,
) -> None:
    """Weight <= 0 is gone for readers (ADR-015) but still visible to
    ``edge_list()`` — the D-40 community-detection input needs the raw rows."""
    await store.upsert_edge("a", "b", "related", {"weight": 0.7})
    await store.upsert_edge("a", "b", "related", {"weight": 0.0})
    assert await store.neighbors("a") == []
    edges = await store.edge_list()
    assert len(edges) == 1
    assert edges[0].weight == 0.0


async def test_edges_of_returns_both_directions(store: GraphStore) -> None:
    await store.upsert_edge("a", "b", "related")
    await store.upsert_edge("c", "a", "derived_from")
    pairs = {(e.src, e.dst) for e in await store.edges_of("a")}
    assert pairs == {("a", "b"), ("c", "a")}


async def test_delete_node_cascades_touching_edges(store: GraphStore) -> None:
    await store.upsert_edge("a", "b", "related")
    await store.upsert_edge("b", "c", "derived_from")
    await store.delete_node("b")
    assert await store.edge_count() == 0
    assert await store.node_count() == 2
    assert await store.neighbors("a") == []


async def test_clear_drops_every_node_and_edge(store: GraphStore) -> None:
    await store.upsert_edge("a", "b", "related")
    await store.clear()
    assert (await store.node_count(), await store.edge_count()) == (0, 0)


async def test_close_is_a_no_op(store: GraphStore) -> None:
    """The injected LadybugClient owns the handle (D-24) — close() must not
    tear anything down that the client still needs."""
    await store.upsert_node("m1")
    await store.close()
    assert await store.node_count() == 1


async def test_unbounded_walk_is_one_native_cypher_query() -> None:
    """KB-5: without a fan-out cap, BFS is one variable-length Cypher query."""
    client = LadybugClient(":memory:")
    await client.connect()
    try:
        store = LadybugGraphStore(client)
        for i in range(10):
            await store.upsert_edge(f"n{i}", f"n{i + 1}", "related", {"weight": 0.5})
        seen: list[str] = []
        real = store._execute

        async def spy(
            query: str, parameters: dict[str, object] | None = None
        ) -> list[list[object]]:
            seen.append(query)
            return await real(query, parameters)

        store._execute = spy  # type: ignore[method-assign]
        found = await store.neighbors("n0", depth=3)
        assert [n.node_id for n in found] == ["n1", "n2", "n3"]
        walks = [q for q in seen if "SHORTEST 1..3" in q]
        assert len(walks) == 1 and len(seen) == 2  # the walk + the node fetch
    finally:
        await client.close()


async def test_pre_kb1_database_is_altered_and_backfilled(tmp_path: object) -> None:
    """A database created with the old two-column rel table gains namespace/
    weight/kind on first use, backfilled from the properties blobs."""
    from pathlib import Path

    path = str(Path(str(tmp_path)) / "old.ladybug")
    client = LadybugClient(path)
    await client.connect()
    conn = client.connection
    conn.execute(
        "CREATE NODE TABLE MemoryNode(node_id STRING, labels STRING, properties STRING, "
        "PRIMARY KEY (node_id))"
    )
    conn.execute(
        "CREATE REL TABLE MemoryLink(FROM MemoryNode TO MemoryNode, rel_type STRING, "
        "properties STRING)"
    )
    conn.execute(
        "CREATE (:MemoryNode {node_id: 'a', labels: '[\"episodic\"]', "
        'properties: \'{"namespace": "ns/a"}\'})'
    )
    conn.execute("CREATE (:MemoryNode {node_id: 'b', labels: '[]', properties: '{}'})")
    conn.execute("CREATE (:MemoryNode {node_id: 'c', labels: '[]', properties: '{}'})")
    conn.execute(
        "MATCH (a:MemoryNode {node_id: 'a'}), (b:MemoryNode {node_id: 'b'}) "
        "CREATE (a)-[:MemoryLink {rel_type: 'related', properties: '{\"weight\": 0.6}'}]->(b)"
    )
    conn.execute(
        "MATCH (a:MemoryNode {node_id: 'a'}), (c:MemoryNode {node_id: 'c'}) "
        "CREATE (a)-[:MemoryLink {rel_type: 'related', properties: '{\"weight\": 0.0}'}]->(c)"
    )
    try:
        store = LadybugGraphStore(client)
        assert [n.node_id for n in await store.neighbors("a", namespace="ns/a")] == ["b"]
        assert {(e.src, e.dst) for e in await store.edge_list("ns/a")} == {("a", "b"), ("a", "c")}
    finally:
        await client.close()


async def test_kuzu_store_is_a_deprecated_alias() -> None:
    """ADR-034: the Kùzu store is the LadybugDB adapter and warns on construction."""
    from memspine.services.graph.kuzu import KuzuGraphStore

    with pytest.warns(DeprecationWarning, match="archived on 2025-10-10"):
        store = KuzuGraphStore(LadybugClient(":memory:"))  # type: ignore[arg-type]
    assert isinstance(store, LadybugGraphStore)
