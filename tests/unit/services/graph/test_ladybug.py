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
