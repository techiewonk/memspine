#!/usr/bin/env python
"""PA-113 graph-filter timing on the current graph/read contracts.

This scope excludes historical RM-110 seed-scan and Engine measures whose
pre-RM-130 name-index assumptions no longer match the runtime. It validates
current qualified walks and filtered graph-leg selection before collecting
timing samples. See the separate PA-113 comparison receipt for source, lock,
host quietness, and baseline/candidate provenance.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import random
import re
import tempfile
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import benchmark_graph_filter as reference

from memspine.read.pipeline.legs import _graph_leg_edges
from memspine.services.graph.base import GraphEdge


async def delete_private_neo4j_nodes(
    driver: Any, database: str, label: str, *, batch_size: int = 100, max_batches: int = 2000
) -> int:
    """Delete only this run's random node label in finite, small transactions."""
    if not re.fullmatch(r"Pa113N[0-9a-f]{10}", label):
        raise ValueError("not a PA-113 private graph label")
    if batch_size < 1 or max_batches < 1:
        raise ValueError("invalid private graph cleanup bounds")
    query = f"MATCH (n:{label}) WITH n LIMIT $limit DETACH DELETE n RETURN count(n) AS removed"

    async def delete_batch(transaction: Any) -> int:
        rows = await transaction.run(query, limit=batch_size)
        row = await rows.single()
        return int(row["removed"])

    removed_total = 0
    async with driver.session(database=database) as session:
        counted = await session.run(f"MATCH (n:{label}) RETURN count(n) AS n")
        initial = int((await counted.single())["n"])
        needed = math.ceil(initial / batch_size) + 1  # terminal zero proves completion
        if needed > max_batches:
            raise RuntimeError("private Neo4j graph exceeds cleanup bound")
        for _ in range(needed):
            removed = await session.execute_write(delete_batch)
            removed_total += removed
            if removed == 0:
                if removed_total != initial:
                    raise RuntimeError("private Neo4j graph changed during cleanup")
                return removed_total
    raise RuntimeError("private Neo4j cleanup exceeded finite batch bound")


async def safe_sqlite_store(data: list[tuple[str, str, str, dict[str, Any]]], path: Path) -> Any:
    from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
    from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph
    from memspine.services.storage.sqlite.schema import metadata

    client = SQLiteClient(SQLiteSpec(str(path)))
    try:
        await client.open()
        async with client.engine.begin() as connection:
            await connection.run_sync(metadata.create_all)
        await reference._sqlite_load(client.engine, data)
    except BaseException as primary:
        try:
            await client.close()
        except BaseException as cleanup_error:
            primary.add_note(f"SQLite cleanup also failed: {type(cleanup_error).__name__}")
            raise primary from cleanup_error
        raise
    return SqlAdjacencyGraph(client), client.close


async def safe_ladybug_store(data: list[tuple[str, str, str, dict[str, Any]]], path: Path) -> Any:
    from memspine.clients.ladybug import LadybugClient, LadybugSpec
    from memspine.services.graph.ladybug import LadybugGraphStore

    client = LadybugClient(LadybugSpec(str(path)))
    try:
        await client.open()
        store = LadybugGraphStore(client)
        await store.node_count()
        await reference._ladybug_load(client, data)
    except BaseException as primary:
        try:
            await client.close()
        except BaseException as cleanup_error:
            primary.add_note(f"Ladybug cleanup also failed: {type(cleanup_error).__name__}")
            raise primary from cleanup_error
        raise
    return store, client.close


async def safe_neo4j_store(data: list[tuple[str, str, str, dict[str, Any]]], _path: Path) -> Any:
    """Register private-label cleanup before writing any benchmark graph rows."""
    from neo4j import AsyncGraphDatabase

    from memspine.services.graph.neo4j import DriverExecutor, Neo4jGraphStore

    url = os.environ["MEMSPINE_TEST_NEO4J_URL"]
    user = os.environ.get("MEMSPINE_TEST_NEO4J_USER")
    auth = (user, os.environ.get("MEMSPINE_TEST_NEO4J_PASSWORD") or "") if user else None
    driver = AsyncGraphDatabase.driver(url, auth=auth)
    label, rel = f"Pa113N{uuid.uuid4().hex[:10]}", f"Pa113R{uuid.uuid4().hex[:10]}"
    try:
        executor = DriverExecutor(driver, node_label=label, rel_type=rel)
        store = Neo4jGraphStore(executor)
    except BaseException as primary:
        try:
            await driver.close()
        except BaseException as cleanup_error:
            primary.add_note(f"Neo4j driver cleanup also failed: {type(cleanup_error).__name__}")
            raise primary from cleanup_error
        raise

    async def close() -> None:
        try:
            await delete_private_neo4j_nodes(driver, executor._database, label)
        finally:
            try:
                async with driver.session(database=executor._database) as session:
                    try:
                        await session.run(f"DROP CONSTRAINT {label}_key IF EXISTS")
                    finally:
                        await session.run(f"DROP INDEX {label}_id IF EXISTS")
            finally:
                await driver.close()

    try:
        await store.node_count()  # May create the private key constraint.
        await reference._neo4j_load(store, data)
    except BaseException as primary:
        try:
            await close()
        except BaseException as cleanup_error:
            primary.add_note(f"private Neo4j cleanup also failed: {type(cleanup_error).__name__}")
            raise primary from cleanup_error
        raise
    return store, close


async def measure_current(store: Any, seeds: list[str], reps: int, leg_reps: int) -> dict[str, Any]:
    """Compare results against the shared walk and namespace scan before timing."""
    edges: list[GraphEdge] = list(await store.edge_list(reference.NS))
    admitted = reference._leg_filter(edges)
    by_id = {
        str(edge.properties["record_id"]): reference._RECORD
        for edge in edges
        if "record_id" in edge.properties
    }
    eng = SimpleNamespace(_graph=store, _shared=None, _storage=reference._NoStorage())
    seed_sets = [list(seeds), *([seed] for seed in seeds)]
    reached: dict[str, list[int]] = {"walk_d1": [], "walk_d2": []}
    module = importlib.import_module(type(store).__module__)
    has_pushdown = hasattr(module, "_walk_plan") or hasattr(
        getattr(store, "_ex", None), "compiles_qualifier_filters"
    )
    for depth in (1, 2):
        for seed in seeds:
            got = await store.neighbors(
                seed, depth=depth, namespace=reference.NS, qualifier_filter=reference.FILTER
            )
            if has_pushdown:
                with reference._shared_walk(store):
                    want = await store.neighbors(
                        seed, depth=depth, namespace=reference.NS, qualifier_filter=reference.FILTER
                    )
            else:
                want = got  # SQLite adjacency already uses the shared path.
            assert [(node.node_id, node.labels, node.properties) for node in got] == [
                (node.node_id, node.labels, node.properties) for node in want
            ], (seed, depth)
            reached[f"walk_d{depth}"].append(len(got))
    sizes = []
    for seed_set in seed_sets:
        got = await _graph_leg_edges(
            eng, reference.NS, seed_set, by_id, reference.FILTER, use_frontier=True
        )
        fetched = await store.frontier_edges(seed_set, namespace=reference.NS)
        reference._check_same_hits(seed_set, admitted, got.edges)
        sizes.append({"seeds": len(seed_set), "fetched": len(fetched), "admitted": len(got.edges)})
    result: dict[str, Any] = {
        "correctness_checked_before_timing": True,
        "walk_parity_mode": "pushdown-vs-shared" if has_pushdown else "shared-only",
        "reached_nodes": {
            key: {"min": min(values), "max": max(values)} for key, values in reached.items()
        },
        "leg_edges": {"namespace": len(edges), "admitted": len(admitted)},
        "leg_frontier_sizes": sizes,
    }
    result.update(await reference.measure_pushdown(store, seeds, reps))
    for depth in (1, 2):
        samples: list[float] = []
        for seed in seeds:

            async def walk(seed: str = seed, depth: int = depth) -> Any:
                return await store.neighbors(
                    seed, depth=depth, namespace=reference.NS, qualifier_filter=reference.FILTER
                )

            samples.extend(await reference._time(walk, max(1, reps // len(seeds)), warmup=1))
        result[f"walk_d{depth}"] = reference._summary(samples)

    async def leg_frontier(seed_set: list[str]) -> Any:
        return await _graph_leg_edges(
            eng, reference.NS, seed_set, by_id, reference.FILTER, use_frontier=True
        )

    async def fetch(seed_set: list[str]) -> Any:
        return await store.frontier_edges(seed_set, namespace=reference.NS)

    frontier_samples: list[float] = []
    fetch_samples: list[float] = []
    for seed_set in seed_sets:
        frontier_samples.extend(
            await reference._time(lambda seed_set=seed_set: leg_frontier(seed_set), leg_reps, 1)
        )
        fetch_samples.extend(
            await reference._time(lambda seed_set=seed_set: fetch(seed_set), leg_reps, 1)
        )
    result["leg_frontier"] = reference._summary(frontier_samples)
    result["leg_frontier_fetch"] = reference._summary(fetch_samples)
    result["within_budget"] = {
        "leg_frontier_p95_le_50ms": result["leg_frontier"]["p95_ms"] <= reference.BUDGET_P95_MS
    }
    if hasattr(store, "_frontier_rows"):
        result.update(await reference.measure_ladybug_frontier(store, seeds, reps, leg_reps))
    return result


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--edges", type=int, default=100_000)
    parser.add_argument("--nodes", type=int, default=20_000)
    parser.add_argument("--mentions", type=int, default=20_000)
    parser.add_argument("--reps", type=int, default=60)
    parser.add_argument("--leg-reps", type=int, default=20)
    parser.add_argument("--seed", type=int, default=96)
    parser.add_argument("--backends", default="sql_adjacency,ladybug,neo4j")
    parser.add_argument("--task", default="PA-113")
    parser.add_argument("--date", default="2026-11-15")
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    assert (
        args.edges > 0
        and args.nodes > 0
        and args.mentions >= 6
        and args.reps > 0
        and args.leg_reps > 0
    )
    started = time.perf_counter()
    data = reference.build_edges(args.edges, args.nodes, args.seed)
    generate_s = time.perf_counter() - started
    rng = random.Random(args.seed + 1)
    hubs = max(1, args.nodes // 100)
    seeds = [reference._node(rng.randrange(hubs, args.nodes)) for _ in range(5)] + [
        reference._node(0)
    ]
    data += reference.build_mentions(args.mentions, args.nodes, args.seed, seeds)
    total = args.edges + args.mentions
    loaders = {"sql_adjacency": safe_sqlite_store}
    skipped = {}
    if importlib.util.find_spec("ladybug") is not None:
        loaders["ladybug"] = safe_ladybug_store
    else:
        skipped["ladybug"] = "ladybug not installed"
    if os.environ.get("MEMSPINE_TEST_NEO4J_URL") and importlib.util.find_spec("neo4j"):
        loaders["neo4j"] = safe_neo4j_store
    else:
        skipped["neo4j"] = "owned Neo4j connection not configured"
    wanted = set(args.backends.split(","))
    assert wanted.issubset(loaders), f"requested backend unavailable: {wanted - set(loaders)}"
    results = {}
    with tempfile.TemporaryDirectory(prefix="pa113-rm96-") as directory:
        for name, loader in loaders.items():
            if name not in wanted:
                continue
            start_load = time.perf_counter()
            store, close = await loader(data, Path(directory) / f"{name}.db")
            try:
                count = len(await store.edge_list(reference.NS))
                assert count == total, (name, count)
                results[name] = {
                    "load_s": round(time.perf_counter() - start_load, 3),
                    "loaded_node_count": int(await store.node_count()),
                    **await measure_current(store, seeds, args.reps, args.leg_reps),
                }
            except BaseException as primary:
                try:
                    await close()
                except BaseException as cleanup_error:
                    primary.add_note(f"{name} cleanup also failed: {type(cleanup_error).__name__}")
                    raise primary from cleanup_error
                raise
            else:
                await close()
            print(
                name,
                "correctness=pass",
                "leg_frontier_p95_ms=",
                results[name]["leg_frontier"]["p95_ms"],
                flush=True,
            )
    versions = {}
    for package in ("sqlalchemy", "aiosqlite", "ladybug", "neo4j", "pydantic"):
        try:
            versions[package] = importlib.metadata.version(package)
        except importlib.metadata.PackageNotFoundError:
            versions[package] = None
    report = {
        "task": args.task,
        "date": args.date,
        "scope": "current qualified graph walks and filtered graph leg; excludes obsolete pre-RM-130 seed-scan and Engine measures",
        "budget": {
            "p95_ms": reference.BUDGET_P95_MS,
            "compiled_edges_target": args.edges,
            "namespace_edges_total": total,
        },
        "graph": {
            "edges": args.edges,
            "mentions_edges": args.mentions,
            "namespace_edges_total": total,
            "requested_entity_nodes": args.nodes,
            "hubs": hubs,
            "generate_s": round(generate_s, 3),
            "seeds": seeds,
        },
        "filter": reference.FILTER.model_dump(mode="json", exclude_none=True),
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
    args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf8", newline="\n")
    print("wrote", args.out, flush=True)


if __name__ == "__main__":
    asyncio.run(main())
