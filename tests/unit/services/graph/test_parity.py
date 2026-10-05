"""Graph-adapter parity (KB-7): every available adapter answers the port identically.

Parametrized over the adapters installable in this environment: SQLite always,
LadybugDB when ``[graph]`` is installed. Kùzu (deprecated, ADR-034) joins only
when it is installed and LadybugDB is not — the two register the same native
types and cannot be imported into one process.

Each walk is checked against a pure-Python reference BFS built on the port's
own rule (:func:`capped_neighbors`), so "parity" means both the recursive CTE
and the Cypher walk agree with one specification, not merely with each other.
Scenarios: a seeded random graph, a hub, tombstones, several namespaces; BFS
depth 1-3 with and without a fan-out cap; the forget cascade; rebuild equality.
"""

from __future__ import annotations

import importlib.util
import random
import sys
from collections.abc import AsyncIterator, Sequence

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.associative.links import link_event
from memspine.memories.associative.projector import GraphProjector
from memspine.services.graph.base import GraphEdge, GraphStore, capped_neighbors
from memspine.services.storage.sqlite.schema import metadata

ADAPTERS = ["sqlite_adjacency", "ladybug", "kuzu"]


def _kuzu_usable() -> bool:
    ladybug_present = "ladybug" in sys.modules or importlib.util.find_spec("ladybug") is not None
    return importlib.util.find_spec("kuzu") is not None and not ladybug_present


@pytest.fixture(params=ADAPTERS)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[GraphStore]:
    if request.param == "kuzu":
        if not _kuzu_usable():
            pytest.skip("kuzu not installed, or ladybug present (one native graph lib per process)")
        from memspine.clients.kuzu import KuzuClient
        from memspine.services.graph.kuzu import KuzuGraphStore

        client = KuzuClient(":memory:")
        await client.connect()
        with pytest.warns(DeprecationWarning):
            kuzu_store = KuzuGraphStore(client)
        yield kuzu_store
        await client.close()
    elif request.param == "ladybug":
        pytest.importorskip("ladybug")
        from memspine.clients.ladybug import LadybugClient
        from memspine.services.graph.ladybug import LadybugGraphStore

        ladybug_client = LadybugClient(":memory:")
        await ladybug_client.connect()
        yield LadybugGraphStore(ladybug_client)
        await ladybug_client.close()
    else:
        sqlite_client = SQLiteClient(":memory:")
        await sqlite_client.connect()
        async with sqlite_client.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph

        yield SQLiteAdjacencyGraph(sqlite_client)
        await sqlite_client.close()


# ── reference walk ────────────────────────────────────────────────────────────


def reference_bfs(
    edges: Sequence[GraphEdge],
    start: str,
    depth: int,
    *,
    namespace: str | None = None,
    namespaces: dict[tuple[str, str, str], str] | None = None,
    rel_type: str | None = None,
    max_degree: int | None = None,
) -> list[str]:
    """Level-by-level BFS, per-node cap, nearest first then by id."""
    live = [
        e
        for e in edges
        if (rel_type is None or e.rel_type == rel_type)
        and (
            namespace is None or (namespaces or {}).get((e.src, e.dst, e.rel_type), "") == namespace
        )
    ]
    hops = {start: 0}
    frontier = [start]
    for level in range(1, depth + 1):
        nxt: list[str] = []
        for node in frontier:
            for other in capped_neighbors(live, node, max_degree):
                if other not in hops:
                    hops[other] = level
                    nxt.append(other)
        frontier = nxt
    del hops[start]
    return sorted(hops, key=lambda node: (hops[node], node))


async def _ids(store: GraphStore, start: str, depth: int, **kw: object) -> list[str]:
    return [n.node_id for n in await store.neighbors(start, depth=depth, **kw)]  # type: ignore[arg-type]


# ── scenarios ─────────────────────────────────────────────────────────────────


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


async def test_neighbors_tombstone_parity(store: GraphStore) -> None:
    """Weight <= 0 is gone for every reader (ADR-015) — identically per adapter."""
    await store.upsert_edge("a", "b", "related", {"weight": 0.7})
    assert {n.node_id for n in await store.neighbors("a")} == {"b"}
    await store.upsert_edge("a", "b", "related", {"weight": 0.0})
    assert await store.neighbors("a") == []
    assert await store.neighbors("b") == []
    # A tombstone stays in the export (prune bookkeeping), just not in walks.
    assert [e.weight for e in await store.edge_list()] == [0.0]
    await store.close()


async def _random_graph(store: GraphStore, seed: int) -> dict[tuple[str, str, str], str]:
    rng = random.Random(seed)
    namespaces: dict[tuple[str, str, str], str] = {}
    for ns in ("ns/a", "ns/b"):
        nodes = [f"{ns[-1]}{i:02d}" for i in range(24)]
        for node in nodes:
            await store.upsert_node(node, labels=["episodic"], properties={"namespace": ns})
        for _ in range(60):
            src, dst = rng.sample(nodes, 2)
            rel = rng.choice(["related", "asserted"])
            # ~15% tombstones; distinct weights keep the cap's ranking strict.
            weight = 0.0 if rng.random() < 0.15 else round(rng.uniform(0.05, 1.0), 4)
            await store.upsert_edge(src, dst, rel, {"weight": weight}, namespace=ns)
            namespaces[(src, dst, rel)] = ns
    return namespaces


@pytest.mark.parametrize("depth", [1, 2, 3])
@pytest.mark.parametrize("max_degree", [None, 2])
async def test_random_graph_bfs_matches_reference(
    store: GraphStore, depth: int, max_degree: int | None
) -> None:
    namespaces = await _random_graph(store, seed=7)
    edges = await store.edge_list()
    for start in ("a00", "a05", "a17", "b03"):
        ns = "ns/a" if start.startswith("a") else "ns/b"
        expected = reference_bfs(edges, start, depth, max_degree=max_degree)
        assert await _ids(store, start, depth, max_degree=max_degree) == expected
        scoped = reference_bfs(
            edges, start, depth, namespace=ns, namespaces=namespaces, max_degree=max_degree
        )
        assert await _ids(store, start, depth, namespace=ns, max_degree=max_degree) == scoped
        by_rel = reference_bfs(edges, start, depth, rel_type="related", max_degree=max_degree)
        assert await _ids(store, start, depth, rel_type="related", max_degree=max_degree) == by_rel
    await store.close()


@pytest.mark.parametrize("depth", [1, 2, 3])
async def test_hub_fan_out_cap(store: GraphStore, depth: int) -> None:
    """A hub with 40 spokes: the cap keeps the strongest spokes, ties by id."""
    for i in range(40):
        await store.upsert_edge("hub", f"s{i:02d}", "related", {"weight": (i % 10 + 1) / 10})
        await store.upsert_edge(f"s{i:02d}", f"leaf{i:02d}", "related", {"weight": 0.5})
    edges = await store.edge_list()
    capped = await _ids(store, "hub", depth, max_degree=3)
    assert capped == reference_bfs(edges, "hub", depth, max_degree=3)
    # The strongest spokes (weight 1.0) are s09, s19, s29, s39 -> first three by id.
    assert capped[:3] == ["s09", "s19", "s29"]
    full = await _ids(store, "hub", depth)
    assert len(full) == (40 if depth == 1 else 80)
    await store.close()


async def test_namespaces_are_isolated(store: GraphStore) -> None:
    """KB-1: a walk, an export and a subgraph never cross into another namespace."""
    await store.upsert_edge("x1", "x2", "related", {"weight": 1.0}, namespace="ns/x")
    await store.upsert_edge("x2", "x3", "related", {"weight": 1.0}, namespace="ns/x")
    # A (forged) bridge edge stored under another namespace.
    await store.upsert_edge("x2", "y1", "related", {"weight": 1.0}, namespace="ns/y")
    await store.upsert_edge("y1", "y2", "related", {"weight": 1.0}, namespace="ns/y")

    assert await _ids(store, "x1", 3, namespace="ns/x") == ["x2", "x3"]
    assert await _ids(store, "x1", 3) == ["x2", "x3", "y1", "y2"]  # unscoped: everything
    assert {(e.src, e.dst) for e in await store.edge_list("ns/x")} == {("x1", "x2"), ("x2", "x3")}
    assert {(e.src, e.dst) for e in await store.edge_list("ns/y")} == {("x2", "y1"), ("y1", "y2")}
    assert await store.edge_list("ns/none") == []

    sub = await store.subgraph(["x1"], 2, namespace="ns/x")
    assert [(e.src, e.dst) for e in sub] == [("x1", "x2"), ("x2", "x3")]
    sub_y = await store.subgraph(["y2"], 5, namespace="ns/y")
    assert [(e.src, e.dst) for e in sub_y] == [("x2", "y1"), ("y1", "y2")]
    await store.close()


async def test_subgraph_drops_tombstones_and_respects_depth(store: GraphStore) -> None:
    await store.upsert_edge("a", "b", "related", {"weight": 0.5})
    await store.upsert_edge("b", "c", "related", {"weight": 0.5})
    await store.upsert_edge("c", "d", "related", {"weight": 0.5})
    await store.upsert_edge("a", "c", "related", {"weight": 0.0})  # tombstone
    one = await store.subgraph(["a"], 1)
    assert [(e.src, e.dst) for e in one] == [("a", "b")]
    two = await store.subgraph(["a"], 2)
    assert [(e.src, e.dst) for e in two] == [("a", "b"), ("b", "c")]
    multi = await store.subgraph(["a", "d"], 1)
    # Induced on the reached set {a, b, c, d}: b-c joins both frontiers.
    assert [(e.src, e.dst) for e in multi] == [("a", "b"), ("b", "c"), ("c", "d")]
    assert await store.subgraph([], 2) == []
    await store.close()


async def test_forget_cascade_parity(store: GraphStore) -> None:
    await _random_graph(store, seed=11)
    victim = "a03"
    touching = await store.edges_of(victim)
    before = await store.edge_count()
    await store.delete_node(victim)
    assert await store.edges_of(victim) == []
    assert await store.edge_count() == before - len(touching)
    for depth in (1, 2, 3):
        for start in ("a00", "a10"):
            assert victim not in await _ids(store, start, depth)
    await store.close()


def _events(namespace: str, seed: int) -> list[MemoryEvent]:
    rng = random.Random(seed)
    records = [
        MemoryRecord(namespace=namespace, memory_type="episodic", content=f"m{i}")
        for i in range(12)
    ]
    events = [
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=namespace,
            actor="test",
            payload={"record": r.model_dump(mode="json")},
        )
        for r in records
    ]
    for _ in range(30):
        src, dst = rng.sample(records, 2)
        weight = 0.0 if rng.random() < 0.2 else round(rng.uniform(0.1, 1.0), 3)
        events.append(
            link_event(namespace, src.record_id, dst.record_id, "related", weight, "test")
        )
    events.append(
        MemoryEvent(
            kind=EventKind.FORGET,
            namespace=namespace,
            actor="test",
            payload={"record_id": records[0].record_id},
        )
    )
    return events


async def _snapshot(store: GraphStore) -> tuple[object, ...]:
    edges = sorted(
        (e.src, e.dst, e.rel_type, e.weight, tuple(sorted(e.properties.items())))
        for e in await store.edge_list()
    )
    per_ns = {ns: len(await store.edge_list(ns)) for ns in ("ns/p", "ns/q")}
    return (await store.node_count(), await store.edge_count(), tuple(edges), per_ns)


async def test_rebuild_reproduces_the_projection(store: GraphStore) -> None:
    """D0.1: clear + replay through the projector reproduces the same graph."""
    projector = GraphProjector(store)
    events = [*_events("ns/p", 3), *_events("ns/q", 4)]
    for event in events:
        await projector.apply(event)
    first = await _snapshot(store)
    await projector.reset()
    assert (await store.node_count(), await store.edge_count()) == (0, 0)
    for event in events:
        await projector.apply(event)
    assert await _snapshot(store) == first
    # Every projected edge lands in its event's namespace, none in limbo.
    assert sum(first[3].values()) == first[1]  # type: ignore[index, union-attr, arg-type]
    await store.close()
