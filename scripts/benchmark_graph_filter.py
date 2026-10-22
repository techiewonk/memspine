#!/usr/bin/env python
"""RM-96 / RM-104: qualifier-filtered graph walks at 100k edges per namespace.

Builds one synthetic namespace of ``--edges`` qualified compiled edges (default 100k over
``--nodes`` entity nodes, with a few hubs) on each available backend and times:

* ``walk_d1`` / ``walk_d2``: ``neighbors(qualifier_filter=...)`` (the shared ``qualified_hops`` walk)
  from typical seeds at depth 1 and 2 under a representative filter;
* ``leg_scan``: the read-path graph leg before RM-104 (and still the unfiltered one):
  load every edge of the namespace with ``edge_list`` and keep the compiled, live ones
  the filter admits; split into ``leg_edge_list`` (the load) and ``leg_filter`` (the
  Python filter);
* ``leg_frontier`` (RM-104): the filtered leg's edge selection as it runs now
  (``_graph_leg_edges``: ``frontier_edges`` over the seeds, compiled/live selection, one
  qualifier parse per edge, filter, caller-scope check), over the read's seed set (all
  six seeds) and each seed alone; ``leg_frontier_fetch`` is the store query alone. Each
  seed set is checked to give one_hop the same hits as the namespace scan.

Budget (owner, 2026-10-22): p95 <= 50 ms for the filtered graph leg at 100k edges.
Backends: SQLite adjacency (always), LadybugDB (when ``ladybug`` imports) and Neo4j when
``MEMSPINE_TEST_NEO4J_URL`` (+ ``_USER`` / ``_PASSWORD``) is set. Bulk loading bypasses
the per-edge upsert path (it is setup, not what is measured) but writes the exact row
layout each adapter's ``upsert_edge`` writes.

    python scripts/benchmark_graph_filter.py [--edges 100000] [--reps 30] [--out PATH]

The RM-96 baseline is docs/provider_verification/rm96-graph-filter-scale-2026-10-22.json.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import importlib.metadata
import importlib.util
import json
import os
import platform
import random
import statistics
import tempfile
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import orjson

from memspine.read.legs.graph import HopEdge, one_hop
from memspine.read.pipeline.legs import _graph_leg_edges, _graph_seeds
from memspine.services.graph.base import GraphEdge
from memspine.services.graph.qualifiers import (
    AssertionModality,
    ClaimState,
    EdgeQualifiers,
    ErasureState,
    QualifierFilter,
    qualified_hops,
    with_qualifiers,
)

NS = "bench/rm96"
COMPILED = "edge_compiler_v2"
BUDGET_P95_MS = 50.0
DOMAINS = ("work", "residence", "health", "family", "travel", "finance", "hobby", "school")
T0 = dt.datetime(2024, 1, 1, tzinfo=dt.UTC)
OUT = Path("docs/provider_verification/rm110-graph-seeds-2026-10-23.json")

#: The representative read filter: asserted, current claims known by now, valid in 2026.
FILTER = QualifierFilter(
    assertion_modalities=frozenset({AssertionModality.ASSERTED}),
    claim_states=frozenset({ClaimState.CURRENT}),
    known_as_of=dt.datetime(2026, 10, 22, tzinfo=dt.UTC),
    valid_at=dt.datetime(2026, 3, 1, tzinfo=dt.UTC),
)


def _node(i: int) -> str:
    return f"ent:{NS}:n{i:06d}"


def build_edges(edges: int, nodes: int, seed: int) -> list[tuple[str, str, str, dict[str, Any]]]:
    """``(src, dst, rel, properties)``; 1% of nodes are hubs drawing 20% of endpoints."""
    rng = random.Random(seed)
    hubs = max(1, nodes // 100)
    seen: set[tuple[str, str, str]] = set()
    out: list[tuple[str, str, str, dict[str, Any]]] = []
    rels = ("knows", "works_on", "lives_in", "related", "cites", "employs")

    def pick() -> int:
        return rng.randrange(hubs) if rng.random() < 0.2 else rng.randrange(nodes)

    while len(out) < edges:
        a, b = pick(), pick()
        if a == b:
            continue
        key = (_node(a), _node(b), rng.choice(rels))
        if key in seen:
            continue
        seen.add(key)
        start = T0 + dt.timedelta(days=rng.randrange(900))
        open_ended = rng.random() < 0.6
        quals = EdgeQualifiers(
            valid_from=start,
            valid_to=None if open_ended else start + dt.timedelta(days=rng.randrange(30, 400)),
            valid_open_ended=True if open_ended else None,
            known_at=start + dt.timedelta(days=rng.randrange(5)),
            assertion_modality=(
                AssertionModality.ASSERTED
                if rng.random() < 0.85
                else AssertionModality.HYPOTHETICAL
            ),
            claim_state=ClaimState.CURRENT if rng.random() < 0.8 else ClaimState.SUPERSEDED,
            erasure_state=ErasureState.ACTIVE,
            scope=NS,
            topical_domain=rng.choice(DOMAINS),
            extraction_confidence=round(rng.random(), 3),
        )
        props = with_qualifiers(
            {
                "weight": round(0.1 + rng.random() * 0.9, 3),
                "compiled": COMPILED,
                "record_id": f"rec-{len(out):06d}",
                "kind": "state",
            },
            quals,
        )
        out.append((*key, props))
    return out


def build_mentions(
    count: int, nodes: int, seed: int, must: Sequence[str]
) -> list[tuple[str, str, str, dict[str, Any]]]:
    """RM-110: ``count`` live ``mentions`` edges record -> entity (the GP-2 entity layer the
    seed lookup indexes), hub-biased like the compiled edges; every ``must`` entity (the
    read's seeds) is mentioned at least once so the query can name it."""
    rng = random.Random(seed + 7)
    hubs = max(1, nodes // 100)
    out: list[tuple[str, str, str, dict[str, Any]]] = []
    targets = list(must)
    while len(out) < count:
        if targets:
            dst = targets.pop()
        else:
            dst = _node(rng.randrange(hubs) if rng.random() < 0.2 else rng.randrange(nodes))
        props = {"weight": round(0.5 + rng.random() * 0.5, 3), "reason": "entity"}
        out.append((f"rec-m{len(out):06d}", dst, "mentions", props))
    return out


def _pct(samples: Sequence[float], q: float) -> float:
    ordered = sorted(samples)
    idx = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[idx]


def _summary(samples_ms: Sequence[float]) -> dict[str, float]:
    return {
        "n": len(samples_ms),
        "p50_ms": round(statistics.median(samples_ms), 3),
        "p95_ms": round(_pct(samples_ms, 0.95), 3),
        "max_ms": round(max(samples_ms), 3),
    }


async def _time(fn: Callable[[], Awaitable[Any]], reps: int, warmup: int = 2) -> list[float]:
    for _ in range(warmup):
        await fn()
    out: list[float] = []
    for _ in range(reps):
        start = time.perf_counter()
        await fn()
        out.append((time.perf_counter() - start) * 1000.0)
    return out


def _leg_filter(edges: Sequence[GraphEdge]) -> list[GraphEdge]:
    """``_graph_hop_leg``'s compiled-edge selection plus the RM-93 filter."""
    compiled = [
        e
        for e in edges
        if e.properties.get("compiled") == COMPILED
        and e.weight > 0
        and e.properties.get("record_id")
    ]
    return [e for e in compiled if FILTER.admits(e)]


async def measure(store: Any, seeds: Sequence[str], reps: int, leg_reps: int) -> dict[str, Any]:
    result: dict[str, Any] = {}
    reached: dict[str, list[int]] = {}
    for depth in (1, 2):
        samples: list[float] = []
        sizes: list[int] = []
        for seed in seeds:

            async def walk(seed: str = seed, depth: int = depth) -> Any:
                return await store.neighbors(
                    seed, depth=depth, namespace=NS, qualifier_filter=FILTER
                )

            sizes.append(len(await walk()))
            samples += await _time(walk, max(1, reps // len(seeds)), warmup=1)
        result[f"walk_d{depth}"] = _summary(samples)
        reached[f"walk_d{depth}"] = sizes

    # RM-96: the depth-2 walk before the hub caps (both caps lifted), same seeds.
    async def fetch_level(frontier: Sequence[str]) -> list[GraphEdge]:
        return list(await store.frontier_edges(frontier, namespace=NS))

    samples, sizes = [], []
    for seed in seeds:

        async def uncapped(seed: str = seed) -> Any:
            return await qualified_hops(
                fetch_level, [seed], 2, FILTER, None, frontier_cap=None, deep_max_degree=None
            )

        sizes.append(len(await uncapped()) - 1)
        samples += await _time(uncapped, max(1, reps // len(seeds)), warmup=1)
    result["walk_d2_uncapped"] = _summary(samples)
    reached["walk_d2_uncapped"] = sizes
    result.update(await measure_seeds(store, seeds, leg_reps))
    edges = await store.edge_list(NS)
    admitted = _leg_filter(edges)
    result["leg_edge_list"] = _summary(await _time(lambda: store.edge_list(NS), leg_reps, 1))
    filter_samples: list[float] = []
    for _ in range(leg_reps):
        start = time.perf_counter()
        _leg_filter(edges)
        filter_samples.append((time.perf_counter() - start) * 1000.0)
    result["leg_filter"] = _summary(filter_samples)

    async def leg() -> list[GraphEdge]:
        return _leg_filter(await store.edge_list(NS))

    result["leg_scan"] = _summary(await _time(leg, leg_reps, 1))

    # RM-104: the leg as it runs now (``_graph_leg_edges``) under the filter, on the seed
    # fetch; ``leg_seed_sets`` are the seed sets of a read (all six seeds, then each alone).
    eng = SimpleNamespace(_graph=store, _shared=None, _storage=_NoStorage())
    by_id = {str(e.properties["record_id"]): _RECORD for e in edges if "record_id" in e.properties}
    seed_sets = [list(seeds), *([s] for s in seeds)]
    frontier_samples: list[float] = []
    fetch_samples: list[float] = []
    sizes: list[dict[str, int]] = []
    for seed_set in seed_sets:

        async def leg_frontier(seed_set: list[str] = seed_set) -> Any:
            return await _graph_leg_edges(eng, NS, seed_set, by_id, FILTER, use_frontier=True)

        async def fetch(seed_set: list[str] = seed_set) -> Any:
            return await store.frontier_edges(seed_set, namespace=NS)

        got = await leg_frontier()
        fetched = await fetch()
        sizes.append({"seeds": len(seed_set), "fetched": len(fetched), "admitted": len(got.edges)})
        _check_same_hits(seed_set, admitted, got.edges)
        frontier_samples += await _time(leg_frontier, max(1, leg_reps), 1)
        fetch_samples += await _time(fetch, max(1, leg_reps), 1)
    result["leg_frontier"] = _summary(frontier_samples)
    result["leg_frontier_fetch"] = _summary(fetch_samples)
    result["leg_frontier_sizes"] = sizes

    # RM-110: the filtered read path's graph part end to end: seeds, then the leg's edges,
    # then one_hop. "before" = seeds from the namespace scan; "after" = maintained index.
    seeds_eng = _seed_engine(store, cached=True)
    scan_eng = _seed_engine(store, cached=False)

    async def read_path(stub: Any) -> Any:
        found = await _graph_seeds(stub, NS, _query(seeds))
        got = await _graph_leg_edges(eng, NS, found, by_id, FILTER)
        return one_hop(
            found,
            [HopEdge(e.src, e.dst, str(e.properties["record_id"])) for e in got.edges],
            min_seeds=1,
        )

    assert (await read_path(seeds_eng)).scores == (await read_path(scan_eng)).scores
    result["read_path_before"] = _summary(await _time(lambda: read_path(scan_eng), leg_reps, 1))
    result["read_path_after"] = _summary(await _time(lambda: read_path(seeds_eng), leg_reps, 1))
    result["reached_nodes"] = {k: {"min": min(v), "max": max(v)} for k, v in reached.items()}
    result["leg_edges"] = {"namespace": len(edges), "admitted": len(admitted)}
    result["within_budget"] = {
        k: result[k]["p95_ms"] <= BUDGET_P95_MS
        for k in (
            "walk_d1",
            "walk_d2",
            "walk_d2_uncapped",
            "leg_scan",
            "leg_frontier",
            "seeds_scan",
            "seeds_cached",
            "read_path_before",
            "read_path_after",
        )
    }
    return result


def _query(seeds: Sequence[str]) -> str:
    """The read's question: names every seed entity (its canonical name is the id suffix)."""
    names = " ".join(seed.rsplit(":", 1)[1] for seed in seeds)
    return f"how are {names} connected"


class _OffsetStorage:
    """The storage slice the entity index reads: a durable projector mark that does not
    move (no other writer during the benchmark) and no records."""

    async def get_offset(self, projector_name: str) -> int:
        return 0

    async def get_record(self, record_id: str) -> None:
        return None


def _seed_engine(store: Any, *, cached: bool) -> Any:
    """An Engine stand-in for ``_graph_seeds``: the real AssociativeMemory over ``store``;
    ``cached=False`` rebuilds the entity index from the namespace scan on every lookup
    (the pre-RM-110 behaviour)."""
    from memspine.memories.associative import entity_cache
    from memspine.memories.associative.store import AssociativeMemory

    async def _no_append(event: Any) -> None:
        return None

    assoc = AssociativeMemory(_OffsetStorage(), store, _no_append)
    if not cached:

        async def scan_lookup(ns: str) -> Any:
            entity_cache.invalidate(store)
            return await entity_cache.entity_lookup(store, None, ns, lambda: assoc.entity_index(ns))

        assoc.entity_lookup = scan_lookup  # type: ignore[method-assign]
    return SimpleNamespace(_associative=assoc, _decision_provider=lambda: None)


async def measure_seeds(store: Any, seeds: Sequence[str], reps: int) -> dict[str, Any]:
    """RM-110: ``_graph_seeds`` before (namespace scan per read) and after (maintained index),
    plus the cold first read that builds the index."""
    from memspine.memories.associative import entity_cache

    query = _query(seeds)
    scan = _seed_engine(store, cached=False)
    cached = _seed_engine(store, cached=True)
    want = await _graph_seeds(scan, NS, query)
    assert sorted(want) == sorted(seeds), want
    entity_cache.invalidate(store)
    start = time.perf_counter()
    got = await _graph_seeds(cached, NS, query)
    cold_ms = (time.perf_counter() - start) * 1000.0
    assert got == want, (got, want)
    return {
        "seeds_scan": _summary(await _time(lambda: _graph_seeds(scan, NS, query), reps, 1)),
        "seeds_cached": _summary(await _time(lambda: _graph_seeds(cached, NS, query), reps * 5, 1)),
        "seeds_cold_build_ms": round(cold_ms, 3),
        "seeds_found": len(got),
    }


class _NoStorage:
    async def get_record(self, record_id: str) -> None:
        return None


#: Every benchmark edge's source record exists and has no viewer restriction.
_RECORD = SimpleNamespace(tags=[])


def _check_same_hits(
    seeds: Sequence[str], scanned: Sequence[GraphEdge], fetched: Sequence[GraphEdge]
) -> None:
    """RM-104 sanity at scale: one_hop over the scan and over the seed fetch agree."""

    def hop(edges: Sequence[GraphEdge]) -> Any:
        res = one_hop(
            seeds,
            [HopEdge(e.src, e.dst, str(e.properties["record_id"])) for e in edges],
            min_seeds=1,
        )
        return res.scores, res.reached

    assert hop(scanned) == hop(fetched), "seed fetch disagrees with the namespace scan"


# ---------------------------------------------------------------------------- loaders
async def sqlite_store(data: list[tuple[str, str, str, dict[str, Any]]], path: Path) -> Any:
    from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
    from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph
    from memspine.services.storage.sqlite.schema import metadata

    client = SQLiteClient(SQLiteSpec(str(path)))
    await client.open()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    await _sqlite_load(client.engine, data)
    return SqlAdjacencyGraph(client), client.close


async def _sqlite_load(engine: Any, data: list[tuple[str, str, str, dict[str, Any]]]) -> None:
    """Bulk rows in the exact layout ``SqlAdjacencyGraph.upsert_edge`` writes."""
    from memspine.services.storage.sqlite.schema import graph_edges, graph_nodes

    async with engine.begin() as conn:
        ids = sorted({x for s, d, _, _ in data for x in (s, d)})
        empty = {"labels": orjson.dumps([]), "properties": orjson.dumps({}), "namespace": NS}
        await conn.execute(graph_nodes.insert(), [{"node_id": i, **empty} for i in ids])
        rows = [
            {
                "src": s,
                "dst": d,
                "rel_type": r,
                "properties": orjson.dumps(p),
                "namespace": NS,
                "weight": float(p["weight"]),
                "kind": p.get("kind"),
            }
            for s, d, r, p in data
        ]
        for i in range(0, len(rows), 10_000):
            await conn.execute(graph_edges.insert(), rows[i : i + 10_000])


async def ladybug_store(data: list[tuple[str, str, str, dict[str, Any]]], path: Path) -> Any:
    from memspine.clients.ladybug import LadybugClient, LadybugSpec
    from memspine.services.graph.ladybug import LadybugGraphStore

    client = LadybugClient(LadybugSpec(str(path)))
    await client.open()
    store = LadybugGraphStore(client)
    await store.node_count()  # runs the DDL
    await _ladybug_load(client, data)
    return store, client.close


async def _ladybug_load(client: Any, data: list[tuple[str, str, str, dict[str, Any]]]) -> None:
    ids = sorted({x for s, d, _, _ in data for x in (s, d)})

    def load() -> None:
        con = client.connection
        for i in range(0, len(ids), 5_000):
            con.execute(
                "UNWIND $rows AS r CREATE (:MemoryNode {node_id: r, labels: '[]', "
                "properties: '{}', namespace: $ns})",
                parameters={"rows": ids[i : i + 5_000], "ns": NS},
            )
        rows = [
            {
                "s": s,
                "d": d,
                "r": r,
                "p": orjson.dumps(p).decode(),
                "w": float(p["weight"]),
                "k": str(p.get("kind") or ""),
            }
            for s, d, r, p in data
        ]
        for i in range(0, len(rows), 5_000):
            con.execute(
                "UNWIND $rows AS x MATCH (a:MemoryNode {node_id: x.s}), "
                "(b:MemoryNode {node_id: x.d}) CREATE (a)-[:MemoryLink {rel_type: x.r, "
                "properties: x.p, namespace: $ns, weight: x.w, kind: x.k}]->(b)",
                parameters={"rows": rows[i : i + 5_000], "ns": NS},
            )

    await asyncio.to_thread(load)


async def neo4j_store(data: list[tuple[str, str, str, dict[str, Any]]], _path: Path) -> Any:
    from neo4j import AsyncGraphDatabase

    from memspine.services.graph.neo4j import DriverExecutor, Neo4jGraphStore

    url = os.environ["MEMSPINE_TEST_NEO4J_URL"]
    user = os.environ.get("MEMSPINE_TEST_NEO4J_USER")
    auth = (user, os.environ.get("MEMSPINE_TEST_NEO4J_PASSWORD") or "") if user else None
    driver = AsyncGraphDatabase.driver(url, auth=auth)
    label, rel = f"Rm96N{uuid.uuid4().hex[:8]}", f"Rm96R{uuid.uuid4().hex[:8]}"
    store = Neo4jGraphStore(DriverExecutor(driver, node_label=label, rel_type=rel), scoped=True)
    await store.node_count()  # creates the key constraint
    sep = "\x1f"
    ids = sorted({x for s, d, _, _ in data for x in (s, d)})
    async with driver.session() as session:
        for i in range(0, len(ids), 5_000):
            await session.run(
                f"UNWIND $rows AS r CREATE (:{label} {{key: $ns + $sep + r, id: r, "
                "namespace: $ns, labels: [], props: '{}', compiled: '', bare: true})",
                rows=ids[i : i + 5_000],
                ns=NS,
                sep=sep,
            )
        rows = [
            {
                "s": NS + sep + s,
                "d": NS + sep + d,
                "key": f"{NS}{sep}{s}{sep}{d}{sep}{r}",
                "r": r,
                "p": json.dumps(p, sort_keys=True, default=str),
                "w": float(p["weight"]),
                "k": str(p.get("kind") or ""),
            }
            for s, d, r, p in data
        ]
        for i in range(0, len(rows), 5_000):
            await session.run(
                f"UNWIND $rows AS x MATCH (a:{label} {{key: x.s}}) MATCH (b:{label} {{key: x.d}}) "
                f"CREATE (a)-[:{rel} {{key: x.key, rel_type: x.r, namespace: $ns, weight: x.w, "
                "kind: x.k, props: x.p}]->(b)",
                rows=rows[i : i + 5_000],
                ns=NS,
            )

    async def close() -> None:
        await store.clear()
        async with driver.session() as session:
            await session.run(f"DROP CONSTRAINT {label}_key IF EXISTS")
        await driver.close()

    return store, close


async def engine_read(
    provider: str,
    data: list[tuple[str, str, str, dict[str, Any]]],
    seeds: Sequence[str],
    reps: int,
    path: Path,
) -> dict[str, Any]:
    """RM-110: a full filtered ``Engine.search`` end to end (every read leg, the seed lookup,
    the RM-104 graph leg and its lift) on an Engine whose graph holds the benchmark world.
    The graph rows are bulk-loaded into the running Engine's own graph store (no source
    records exist, so the leg's caller check looks each one up and drops it: the leg's full
    cost with no lift). "before" swaps in the namespace-scan seed lookup on every read."""
    from memspine import Engine
    from memspine.memories.associative import entity_cache

    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(path), "busy_timeout_s": 5.0},
            },
            "graph": {"provider": provider},
        },
        embedding={"provider": "hash", "settings": {}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        graph={"edge_compiler_v2": True},
        read={
            "hybrid": True,
            "record_access": False,
            "legs_v2": True,
            "graph_leg_v2": True,
            "graph_leg_seed_hits": 0,
        },
    )
    await eng.start()
    try:
        graph = eng._graph
        query = _query(seeds)
        # the Engine's fixed read cost (every other leg, no graph world yet)
        empty = await _time(
            lambda: eng.search(query, namespace=NS, top_k=8, graph_filter=FILTER), reps, 1
        )
        if provider == "sql_adjacency":
            await _sqlite_load(graph._client.engine, data)
        else:
            await graph.node_count()
            await _ladybug_load(graph._client, data)
        assoc = eng._associative
        frontier_calls: list[int] = []
        real_frontier = graph.frontier_edges

        async def counting_frontier(nodes: Sequence[str], **kw: Any) -> Any:
            frontier_calls.append(len(nodes))
            return await real_frontier(nodes, **kw)

        graph.frontier_edges = counting_frontier

        async def search() -> Any:
            return await eng.search(query, namespace=NS, top_k=8, graph_filter=FILTER)

        entity_cache.invalidate(graph)
        start = time.perf_counter()
        await search()
        cold_ms = (time.perf_counter() - start) * 1000.0
        assert sorted(await eng._graph_seeds(NS, query)) == sorted(seeds)
        frontier_calls.clear()
        await search()
        assert frontier_calls, "the filtered graph leg did not run"
        after = await _time(search, reps, 1)
        real_lookup = assoc.entity_lookup

        async def scan_lookup(ns: str) -> Any:
            entity_cache.invalidate(graph)
            return await real_lookup(ns)

        assoc.entity_lookup = scan_lookup
        before = await _time(search, max(3, reps // 4), 1)
        assoc.entity_lookup = real_lookup
        return {
            "engine_search_empty_graph": _summary(empty),
            "engine_search_before": _summary(before),
            "engine_search_after": _summary(after),
            "engine_search_cold_first_ms": round(cold_ms, 3),
            "engine_leg_seed_fetch_nodes": frontier_calls[0],
            "within_budget": {
                "engine_search_before": _pct(before, 0.95) <= BUDGET_P95_MS,
                "engine_search_after": _pct(after, 0.95) <= BUDGET_P95_MS,
            },
        }
    finally:
        await eng.stop()


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", type=int, default=100_000)
    parser.add_argument("--nodes", type=int, default=20_000)
    parser.add_argument("--reps", type=int, default=60, help="walk timings per depth")
    parser.add_argument("--leg-reps", type=int, default=20, help="read-path leg timings")
    parser.add_argument("--seed", type=int, default=96)
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--task", default="RM-110")
    parser.add_argument("--date", default="2026-10-23")
    parser.add_argument("--mentions", type=int, default=20_000, help="entity mentions edges")
    parser.add_argument("--engine-reps", type=int, default=20, help="Engine.search timings")
    parser.add_argument("--backends", default="sql_adjacency,ladybug,neo4j")
    args = parser.parse_args()

    started = time.perf_counter()
    data = build_edges(args.edges, args.nodes, args.seed)
    gen_s = time.perf_counter() - started
    rng = random.Random(args.seed + 1)
    hubs = max(1, args.nodes // 100)
    seeds = [_node(rng.randrange(hubs, args.nodes)) for _ in range(5)] + [_node(0)]
    data += build_mentions(args.mentions, args.nodes, args.seed, seeds)
    total = args.edges + args.mentions

    loaders: dict[str, Callable[..., Awaitable[Any]]] = {"sql_adjacency": sqlite_store}
    skipped: dict[str, str] = {}
    if importlib.util.find_spec("ladybug") is not None:
        loaders["ladybug"] = ladybug_store
    else:
        skipped["ladybug"] = "ladybug not installed"
    if os.environ.get("MEMSPINE_TEST_NEO4J_URL") and importlib.util.find_spec("neo4j"):
        loaders["neo4j"] = neo4j_store
    else:
        skipped["neo4j"] = "MEMSPINE_TEST_NEO4J_URL not set (optional)"
    wanted = set(args.backends.split(","))

    results: dict[str, Any] = {}
    with tempfile.TemporaryDirectory(prefix="rm96-") as tmp:
        for name, loader in loaders.items():
            if name not in wanted:
                continue
            t_load = time.perf_counter()
            store, close = await loader(data, Path(tmp) / f"{name}.db")
            load_s = time.perf_counter() - t_load
            try:
                count = len(await store.edge_list(NS))
                assert count == total, (name, count)
                results[name] = {
                    "load_s": round(load_s, 1),
                    **await measure(store, seeds, args.reps, args.leg_reps),
                }
            finally:
                await close()
            if name in ("sql_adjacency", "ladybug") and args.engine_reps > 0:
                read = await engine_read(
                    name, data, seeds, args.engine_reps, Path(tmp) / f"engine-{name}.db"
                )
                results[name]["within_budget"].update(read.pop("within_budget"))
                results[name].update(read)
            print(name, json.dumps(results[name], indent=1))

    versions = {}
    for pkg in ("sqlalchemy", "aiosqlite", "ladybug", "neo4j", "pydantic"):
        try:
            versions[pkg] = importlib.metadata.version(pkg)
        except importlib.metadata.PackageNotFoundError:
            versions[pkg] = None
    report = {
        "task": args.task,
        "date": args.date,
        "budget": {"p95_ms": BUDGET_P95_MS, "edges_per_namespace": args.edges},
        "graph": {
            "edges": args.edges,
            "mentions_edges": args.mentions,
            "namespace_edges_total": total,
            "nodes": args.nodes,
            "hubs": max(1, args.nodes // 100),
            "hub_endpoint_share": 0.2,
            "generate_s": round(gen_s, 1),
            "seeds": seeds,
        },
        "filter": FILTER.model_dump(mode="json", exclude_none=True),
        "measures": {
            "walk_d1/walk_d2": "neighbors(qualifier_filter=...) (qualified_hops) from each seed",
            "leg_edge_list": "edge_list(namespace): the read-path graph leg's load",
            "leg_filter": "compiled/live selection + QualifierFilter.admits over that list",
            "leg_scan": "leg_edge_list + leg_filter (the leg before RM-104; unfiltered reads)",
            "leg_frontier": "_graph_leg_edges on frontier_edges(seeds) (the filtered leg now)",
            "leg_frontier_fetch": "frontier_edges(seeds, namespace) alone",
            "walk_d2_uncapped": "qualified_hops depth 2 with both RM-96 caps lifted (before)",
            "seeds_scan": "_graph_seeds with the entity index rebuilt from edge_list (before)",
            "seeds_cached": "_graph_seeds on the maintained entity index (RM-110, after)",
            "seeds_cold_build_ms": "the first read of a namespace: one index build",
            "read_path_before/after": "seeds + _graph_leg_edges (frontier) + one_hop",
            "engine_search_before/after": "Engine.search with graph_filter end to end",
        },
        "results": results,
        "skipped": skipped,
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "machine": platform.machine(),
            "versions": versions,
        },
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
