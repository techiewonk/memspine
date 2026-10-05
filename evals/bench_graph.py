"""KB-8 graph benchmark (#24): BFS, PPR and community detection on synthetic graphs.

CPU only, no model calls, not part of the test suite (a 200-edge smoke test
lives in ``tests/test_bench_graph.py``). For each size it builds a synthetic
graph with planted communities (power-law sizes 10-400, average degree ~12,
a share ``mu`` of each node's edges leaving its community, weights 0.3-1.0),
bulk-loads it into each graph store, and measures:

* **BFS** ``neighbors(seed, depth=d)`` for d = 1..3, p50/p95 over random seeds,
  on ``sqlite_adjacency`` and, when ``ladybug`` imports, on LadybugDB;
* **PPR**: ``edge_list`` export time and ``personalized_pagerank`` p50/p95;
* **communities**: ``partition_graph`` time for Leiden (when ``[community]`` is
  installed) and LPA, community count, modularity (Leiden extra only), and
  determinism (a shuffled edge list must give the same partition).

Usage::

    python evals/bench_graph.py                        # 10K and 100K edges
    python evals/bench_graph.py --million              # adds 1M edges
    python evals/bench_graph.py --sizes 10000 --out runs/bench_graph

Writes ``bench_graph.json`` and ``bench_graph.md`` into ``--out``
(default ``evals/runs/bench_graph``, git-ignored).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from memspine.clients.sqlite import SQLiteClient
from memspine.memories.associative.communities import (
    canonical_edges,
    communities_available,
    partition_graph,
)
from memspine.memories.associative.ppr import personalized_pagerank
from memspine.services.graph.base import GraphEdge, GraphStore
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.storage.sqlite.schema import graph_edges, graph_nodes

NAMESPACE = "bench"
DEFAULT_SIZES = (10_000, 100_000)
MILLION = 1_000_000
BATCH = 5_000


def synthetic_graph(
    target_edges: int, *, mu: float = 0.3, seed: int = 1, avg_degree: int = 12
) -> list[tuple[str, str, float]]:
    """Canonical ``(src, dst, weight)`` edges with planted communities."""
    rng = random.Random(seed)
    n_nodes = max(10, target_edges * 2 // avg_degree)
    sizes: list[int] = []
    while sum(sizes) < n_nodes:
        sizes.append(max(10, min(400, int(rng.paretovariate(1.5) * 15))))
    members: list[list[str]] = []
    k = 0
    for size in sizes:
        members.append([f"n{k + i:07d}" for i in range(size)])
        k += size
    every = [node for group in members for node in group]
    edges: dict[tuple[str, str], float] = {}
    attempts = 0
    while len(edges) < target_edges and attempts < target_edges * 4:
        attempts += 1
        group = members[rng.randrange(len(members))]
        a = rng.choice(group)
        b = rng.choice(every) if rng.random() < mu else rng.choice(group)
        if a == b:
            continue
        pair = (a, b) if a < b else (b, a)
        edges.setdefault(pair, round(rng.uniform(0.3, 1.0), 3))
    return sorted((a, b, w) for (a, b), w in edges.items())


def _graph_edges(tuples: list[tuple[str, str, float]]) -> list[GraphEdge]:
    return [GraphEdge(a, b, "related", {"weight": w}) for a, b, w in tuples]


def _percentiles(samples: list[float]) -> dict[str, float]:
    ordered = sorted(samples)
    p95 = ordered[min(len(ordered) - 1, round(0.95 * (len(ordered) - 1)))]
    return {
        "p50_ms": round(statistics.median(ordered) * 1000, 2),
        "p95_ms": round(p95 * 1000, 2),
        "n": len(ordered),
    }


async def _timed(samples: list[float], call: Callable[[], Awaitable[Any]]) -> Any:
    start = time.perf_counter()
    out = await call()
    samples.append(time.perf_counter() - start)
    return out


async def _load_sqlite(tuples: list[tuple[str, str, float]]) -> tuple[SQLiteAdjacencyGraph, Any]:
    """Bulk insert straight into the projection tables (the per-edge upsert
    path is not what is being measured here)."""
    import orjson

    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    nodes = sorted({n for a, b, _w in tuples for n in (a, b)})
    async with client.engine.begin() as conn:
        for i in range(0, len(nodes), BATCH):
            await conn.execute(
                graph_nodes.insert(),
                [
                    {"node_id": n, "labels": b"[]", "properties": b"{}", "namespace": NAMESPACE}
                    for n in nodes[i : i + BATCH]
                ],
            )
        for i in range(0, len(tuples), BATCH):
            await conn.execute(
                graph_edges.insert(),
                [
                    {
                        "src": a,
                        "dst": b,
                        "rel_type": "related",
                        "properties": orjson.dumps({"weight": w}),
                        "namespace": NAMESPACE,
                        "weight": w,
                    }
                    for a, b, w in tuples[i : i + BATCH]
                ],
            )
    return SQLiteAdjacencyGraph(client), client


async def _load_ladybug(
    tuples: list[tuple[str, str, float]], directory: str
) -> tuple[GraphStore, Any] | None:
    try:
        import ladybug  # noqa: F401
    except ImportError:
        return None
    import orjson

    from memspine.clients.ladybug import LadybugClient
    from memspine.services.graph.ladybug import LadybugGraphStore

    client = LadybugClient(str(Path(directory) / "bench.lbug"))
    await client.connect()
    store = LadybugGraphStore(client)
    await store.edge_count()  # runs the DDL
    conn = client.connection
    nodes = sorted({n for a, b, _w in tuples for n in (a, b)})
    for i in range(0, len(nodes), BATCH):
        conn.execute(
            "UNWIND $rows AS r CREATE (:MemoryNode {node_id: r, labels: '[]', "
            "properties: '{}', namespace: $ns})",
            parameters={"rows": nodes[i : i + BATCH], "ns": NAMESPACE},
        )
    for i in range(0, len(tuples), BATCH):
        rows = [
            {"s": a, "d": b, "w": w, "p": orjson.dumps({"weight": w}).decode()}
            for a, b, w in tuples[i : i + BATCH]
        ]
        conn.execute(
            "UNWIND $rows AS r MATCH (a:MemoryNode {node_id: r.s}), (b:MemoryNode {node_id: r.d}) "
            "CREATE (a)-[:MemoryLink {rel_type: 'related', properties: r.p, namespace: $ns, "
            "weight: r.w}]->(b)",
            parameters={"rows": rows, "ns": NAMESPACE},
        )
    return store, client


async def _bench_bfs(
    store: GraphStore, seeds: list[str], depths: tuple[int, ...]
) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for depth in depths:
        samples: list[float] = []
        reached: list[int] = []
        for seed in seeds:
            hits = await _timed(
                samples, lambda s=seed, d=depth: store.neighbors(s, depth=d, namespace=NAMESPACE)
            )
            reached.append(len(hits))
        out[f"depth_{depth}"] = {**_percentiles(samples), "mean_reached": statistics.mean(reached)}
    return out


def _bench_communities(edges: list[GraphEdge], *, seed: int) -> dict[str, Any]:
    out: dict[str, Any] = {}
    shuffled = list(edges)
    random.Random(seed).shuffle(shuffled)
    algorithms = ["lpa"] + (["leiden"] if communities_available() else [])
    for algorithm in algorithms:
        start = time.perf_counter()
        result = partition_graph(edges, algorithm=algorithm)  # type: ignore[arg-type]
        elapsed = time.perf_counter() - start
        again = partition_graph(shuffled, algorithm=algorithm)  # type: ignore[arg-type]
        sizes = [len(c) for c in result.communities()]
        row: dict[str, Any] = {
            "seconds": round(elapsed, 3),
            "communities": len(sizes),
            "largest": max(sizes, default=0),
            "collapsed": result.collapsed,
            "order_independent": again.labels == result.labels,
        }
        if communities_available() and result.labels:
            import graspologic_native as gn

            row["modularity"] = round(
                gn.modularity(canonical_edges(edges), result.labels, resolution=1.0), 4
            )
        out[algorithm] = row
    return out


async def bench_size(
    target_edges: int,
    *,
    queries: int = 50,
    ppr_queries: int = 5,
    ladybug: bool = True,
    communities: bool = True,
    seed: int = 1,
) -> dict[str, Any]:
    """Every measurement for one graph size."""
    tuples = synthetic_graph(target_edges, seed=seed)
    edges = _graph_edges(tuples)
    nodes = sorted({n for a, b, _w in tuples for n in (a, b)})
    rng = random.Random(seed)
    seeds = [rng.choice(nodes) for _ in range(queries)]
    report: dict[str, Any] = {"edges": len(tuples), "nodes": len(nodes), "stores": {}}
    depths = (1, 2, 3)

    start = time.perf_counter()
    sqlite_store, sqlite_client = await _load_sqlite(tuples)
    load = time.perf_counter() - start
    try:
        export: list[float] = []
        exported = await _timed(export, lambda: sqlite_store.edge_list(NAMESPACE))
        report["stores"]["sqlite_adjacency"] = {
            "load_seconds": round(load, 3),
            "bfs": await _bench_bfs(sqlite_store, seeds, depths),
            "edge_list_ms": round(export[0] * 1000, 2),
        }
    finally:
        await sqlite_client.close()

    if ladybug:
        with tempfile.TemporaryDirectory() as directory:
            start = time.perf_counter()
            loaded = await _load_ladybug(tuples, directory)
            if loaded is not None:
                store, client = loaded
                try:
                    report["stores"]["ladybug"] = {
                        "load_seconds": round(time.perf_counter() - start, 3),
                        "bfs": await _bench_bfs(store, seeds, depths),
                    }
                finally:
                    await client.close()

    ppr: list[float] = []
    for query_seed in seeds[:ppr_queries]:
        start = time.perf_counter()
        personalized_pagerank(exported, {query_seed}, top_k=20)
        ppr.append(time.perf_counter() - start)
    report["ppr"] = _percentiles(ppr)
    if communities:
        report["communities"] = _bench_communities(edges, seed=seed)
    return report


def to_markdown(report: dict[str, Any]) -> str:
    lines = [
        "# Graph benchmark (KB-8, #24)",
        "",
        f"Synthetic planted-community graphs, CPU only. Python {report['python']}; "
        f"graspologic-native {'installed' if report['leiden'] else 'not installed'}.",
        "",
        "| edges | nodes | store | load s | BFS d1 p50/p95 ms | BFS d2 p50/p95 ms | "
        "BFS d3 p50/p95 ms | d3 reached |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for size in report["sizes"]:
        for name, store in size["stores"].items():
            bfs = store["bfs"]
            cells = [
                f"{bfs[f'depth_{d}']['p50_ms']} / {bfs[f'depth_{d}']['p95_ms']}" for d in (1, 2, 3)
            ]
            lines.append(
                f"| {size['edges']} | {size['nodes']} | {name} | {store['load_seconds']} | "
                + " | ".join(cells)
                + f" | {bfs['depth_3']['mean_reached']:.0f} |"
            )
    lines += [
        "",
        "| edges | edge_list ms | PPR p50/p95 ms | algorithm | seconds | communities | "
        "largest | modularity | order-independent | collapsed |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for size in report["sizes"]:
        export = size["stores"]["sqlite_adjacency"]["edge_list_ms"]
        ppr = f"{size['ppr']['p50_ms']} / {size['ppr']['p95_ms']}"
        for algorithm, row in size.get("communities", {}).items():
            lines.append(
                f"| {size['edges']} | {export} | {ppr} | {algorithm} | {row['seconds']} | "
                f"{row['communities']} | {row['largest']} | {row.get('modularity', '-')} | "
                f"{'yes' if row['order_independent'] else 'NO'} | {row['collapsed']} |"
            )
    return "\n".join(lines) + "\n"


async def run(
    sizes: list[int],
    *,
    queries: int = 50,
    ppr_queries: int = 5,
    ladybug: bool = True,
    communities: bool = True,
    seed: int = 1,
) -> dict[str, Any]:
    results = []
    for size in sizes:
        print(f"[bench_graph] {size} edges ...", file=sys.stderr, flush=True)
        results.append(
            await bench_size(
                size,
                queries=queries,
                ppr_queries=ppr_queries,
                ladybug=ladybug,
                communities=communities,
                seed=seed,
            )
        )
    return {
        "python": sys.version.split()[0],
        "leiden": communities_available(),
        "seed": seed,
        "sizes": results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sizes", default=",".join(str(s) for s in DEFAULT_SIZES))
    parser.add_argument("--million", action="store_true", help="also run 1M edges")
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--ppr-queries", type=int, default=5)
    parser.add_argument("--no-ladybug", action="store_true")
    parser.add_argument("--no-communities", action="store_true")
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--out", default=str(Path(__file__).parent / "runs" / "bench_graph"))
    args = parser.parse_args(argv)
    sizes = [int(s) for s in args.sizes.split(",") if s]
    if args.million:
        sizes.append(MILLION)
    report = asyncio.run(
        run(
            sizes,
            queries=args.queries,
            ppr_queries=args.ppr_queries,
            ladybug=not args.no_ladybug,
            communities=not args.no_communities,
            seed=args.seed,
        )
    )
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "bench_graph.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    markdown = to_markdown(report)
    (out / "bench_graph.md").write_text(markdown, encoding="utf-8")
    print(markdown)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
