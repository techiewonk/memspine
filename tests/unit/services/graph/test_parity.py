"""Graph-adapter parity: every available adapter answers the port identically.

Parametrized over the adapters installable in this environment; kuzu joins
automatically once ``pip install memspine[kuzu]`` is present (importorskip).
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.services.graph.base import GraphStore
from memspine.services.storage.sqlite.schema import metadata

ADAPTERS = ["sqlite_adjacency", "kuzu"]


@pytest.fixture(params=ADAPTERS)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[GraphStore]:
    if request.param == "kuzu":
        pytest.importorskip("kuzu")
        from memspine.clients.kuzu import KuzuClient
        from memspine.services.graph.kuzu import KuzuGraphStore

        client = KuzuClient(":memory:")
        await client.connect()
        yield KuzuGraphStore(client)
        await client.close()
    else:
        sqlite_client = SQLiteClient(":memory:")
        await sqlite_client.connect()
        async with sqlite_client.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph

        yield SQLiteAdjacencyGraph(sqlite_client)
        await sqlite_client.close()


async def test_port_round_trip_parity(store: GraphStore) -> None:
    """One scenario, identical observable answers from every adapter."""
    await store.upsert_node("m1", labels=["memory"], properties={"kind": "fact"})
    await store.upsert_edge("m1", "m2", "related", {"weight": 0.8})
    await store.upsert_edge("m2", "m3", "derived_from")
    await store.upsert_edge("m1", "m2", "related", {"weight": 0.9})  # idempotent replace

    assert await store.node_count() == 3
    assert await store.edge_count() == 2

    assert {n.node_id for n in await store.neighbors("m1")} == {"m2"}
    assert {n.node_id for n in await store.neighbors("m1", depth=2)} == {"m2", "m3"}
    assert {n.node_id for n in await store.neighbors("m2", rel_type="related")} == {"m1"}

    edges = {(e.src, e.dst, e.rel_type, e.weight) for e in await store.edge_list()}
    assert edges == {("m1", "m2", "related", 0.9), ("m2", "m3", "derived_from", 1.0)}
    assert {(e.src, e.dst) for e in await store.edges_of("m2")} == {("m1", "m2"), ("m2", "m3")}

    await store.delete_node("m2")
    assert await store.edge_count() == 0
    assert await store.neighbors("m1") == []

    await store.clear()
    assert (await store.node_count(), await store.edge_count()) == (0, 0)
    await store.close()
