"""Neo4j graph store (F-16e, M-04 / M-18, owner Q4): the server profile's graph backend.

The store is a ``GraphStore`` over a small executor port, so the Cypher lives in one table
(:data:`CYPHER`) and a test can stand in an in-memory MERGE recorder for the driver. It is only
ever a *projection*: nothing writes to it directly. The engine feeds it through the durable
outbox (``graph.neo4j_outbox``, stage ``graph_projection``, see
:mod:`memspine.services.graph.neo4j_outbox`), and every mutation is an idempotent MERGE.

Model. One node label and one relationship type carry everything; the memory-level labels and
edge relation ride as properties (so no user text is ever spliced into Cypher):

``(:MemNode {key, id, namespace, labels, props, compiled, bare})``
``(:MemNode)-[:MemRel {key, rel_type, namespace, weight, kind, props}]->(:MemNode)``

Keys. Unscoped (today's keys, parity with ``sqlite_adjacency``): a node is keyed by its id and an
edge by ``(src, dst, rel_type)``. With ``isolation.scope_keys_v2`` (``scoped=True``) both include
the namespace, so two tenants never MERGE onto one node or edge. ``delete_node`` and ``edges_of``
take a bare node id (the port has no namespace there) and match on the ``id`` property.

Edges with weight ``<= 0`` are the ADR-015 tombstone: stored, never traversed. Constructing the
store without an executor raises the D-10 error naming the ``[neo4j]`` extra (the config value
``graph.provider: neo4j`` as a *primary* store stays unsupported).
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

from memspine.exceptions import MissingServiceError
from memspine.observability import tap
from memspine.services.graph.base import (
    GraphEdge,
    GraphNode,
    capped_neighbors,
    edge_kind,
    edge_namespace,
    edge_weight,
)

__all__ = [
    "CYPHER",
    "DriverExecutor",
    "Neo4jExecutor",
    "Neo4jGraphStore",
    "executor_from_env",
]

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
_SEP = "\x1f"

#: op name -> Cypher. ``{N}`` / ``{R}`` are the (validated) node label and relationship type.
CYPHER: dict[str, str] = {
    "schema": "CREATE CONSTRAINT {N}_key IF NOT EXISTS FOR (n:{N}) REQUIRE n.key IS UNIQUE",
    "upsert_node": (
        "MERGE (n:{N} {{key: $key}}) "
        "SET n.id = $id, n.namespace = $ns, n.labels = $labels, n.props = $props, "
        "n.compiled = $compiled, n.bare = false"
    ),
    "upsert_edge": (
        "MERGE (a:{N} {{key: $skey}}) "
        "ON CREATE SET a.id = $src, a.namespace = $ns, a.labels = [], a.props = '{{}}', "
        "a.compiled = '', a.bare = true "
        "MERGE (b:{N} {{key: $dkey}}) "
        "ON CREATE SET b.id = $dst, b.namespace = $ns, b.labels = [], b.props = '{{}}', "
        "b.compiled = '', b.bare = true "
        "MERGE (a)-[r:{R} {{key: $ekey}}]->(b) "
        "SET r.rel_type = $rel, r.namespace = $ns, r.weight = $weight, r.kind = $kind, "
        "r.props = $props"
    ),
    # Delete in three statements (a read of a deleted node's edges inside one query is unsafe):
    # the bare neighbours first, then the node with its edges, then the bare ones left edgeless.
    "bare_neighbours": (
        "MATCH (n:{N})-[:{R}]-(m:{N}) WHERE n.id = $id AND m.bare = true AND m.id <> $id "
        "RETURN DISTINCT m.id AS id"
    ),
    "delete_node": "MATCH (n:{N}) WHERE n.id = $id DETACH DELETE n",
    "drop_bare": (
        "MATCH (m:{N}) WHERE m.id IN $ids AND m.bare = true AND NOT (m)-[:{R}]-() DELETE m"
    ),
    # Compiled nodes (assertion / value / time) left with no live edge go with a retraction.
    "prune_dead": (
        "MATCH (n:{N}) WHERE n.id IN $ids AND n.compiled = $compiled "
        "AND NOT EXISTS {{ MATCH (n)-[r:{R}]-() WHERE r.weight > 0 }} "
        "DETACH DELETE n"
    ),
    "edges_of": (
        "MATCH (a:{N})-[r:{R}]->(b:{N}) WHERE a.id IN $ids OR b.id IN $ids "
        "RETURN a.id AS src, b.id AS dst, r.rel_type AS rel, r.namespace AS ns, "
        "r.props AS props"
    ),
    "edge_list": (
        "MATCH (a:{N})-[r:{R}]->(b:{N}) WHERE $ns IS NULL OR r.namespace = $ns "
        "RETURN a.id AS src, b.id AS dst, r.rel_type AS rel, r.namespace AS ns, "
        "r.props AS props"
    ),
    "nodes": (
        "MATCH (n:{N}) WHERE n.id IN $ids "
        "RETURN n.id AS id, n.labels AS labels, n.props AS props, n.namespace AS ns"
    ),
    "node_count": "MATCH (n:{N}) RETURN count(n) AS c",
    "edge_count": "MATCH (:{N})-[r:{R}]->(:{N}) RETURN count(r) AS c",
    "clear": "MATCH (n:{N}) DETACH DELETE n",
}


class Neo4jExecutor(Protocol):
    """Runs one named op with parameters and returns its rows (dicts). The live one runs the
    :data:`CYPHER` text in a write transaction; a test supplies an in-memory recorder."""

    async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


def _check_ident(value: str, what: str) -> str:
    if not _IDENT.match(value):
        raise ValueError(f"invalid neo4j {what} {value!r}")
    return value


class DriverExecutor:
    """:class:`Neo4jExecutor` over the official async driver (``[neo4j]``)."""

    def __init__(
        self,
        driver: Any,
        *,
        database: str = "neo4j",
        node_label: str = "MemNode",
        rel_type: str = "MemRel",
    ) -> None:
        self._driver = driver
        self._database = database
        self._fmt = {"N": _check_ident(node_label, "label"), "R": _check_ident(rel_type, "type")}

    async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        text = CYPHER[op].format(**self._fmt)

        async def work(tx: Any) -> list[dict[str, Any]]:
            result = await tx.run(text, dict(params))
            return [dict(record) async for record in result]

        async with self._driver.session(database=self._database) as session:
            return list(await session.execute_write(work))

    async def close(self) -> None:
        await self._driver.close()


def executor_from_env(
    *,
    url_env: str,
    user_env: str,
    password_env: str,
    database: str = "neo4j",
    node_label: str = "MemNode",
    rel_type: str = "MemRel",
) -> DriverExecutor:
    """Build the live executor from environment variables named by config (never from values in
    the config file). Raises :class:`MissingServiceError` without the driver, ``ValueError``
    without the URL variable."""
    try:
        from neo4j import AsyncGraphDatabase  # type: ignore[import-not-found,unused-ignore]
    except ImportError as exc:
        raise MissingServiceError("graph:neo4j", extra="neo4j") from exc
    url = os.environ.get(url_env)
    if not url:
        raise ValueError(f"graph.neo4j_outbox needs the environment variable {url_env} (the URL)")
    user, password = os.environ.get(user_env), os.environ.get(password_env)
    auth = (user, password or "") if user else None
    return DriverExecutor(
        AsyncGraphDatabase.driver(url, auth=auth),
        database=database,
        node_label=node_label,
        rel_type=rel_type,
    )


def _jsonable(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, sort_keys=True))


def _dump(value: Mapping[str, object] | None) -> str:
    return json.dumps(_jsonable(dict(value or {})), sort_keys=True)


def _load(raw: object) -> dict[str, object]:
    if isinstance(raw, str) and raw:
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    return {}


class Neo4jGraphStore:
    """``GraphStore`` over a :class:`Neo4jExecutor`. See the module docstring."""

    def __init__(self, executor: Neo4jExecutor | None = None, *, scoped: bool = False) -> None:
        if executor is None:
            raise MissingServiceError("graph:neo4j", extra="neo4j")
        self._ex = executor
        self.scoped = scoped
        self._schema_done = False

    # -- keys ------------------------------------------------------------------------------
    def node_key(self, node_id: str, namespace: str) -> str:
        return f"{namespace}{_SEP}{node_id}" if self.scoped else node_id

    def edge_key(self, src: str, dst: str, rel: str, namespace: str) -> str:
        prefix = f"{namespace}{_SEP}" if self.scoped else ""
        return f"{prefix}{src}{_SEP}{dst}{_SEP}{rel}"

    async def _run(self, op: str, **params: Any) -> list[dict[str, Any]]:
        if not self._schema_done:
            self._schema_done = True
            await self._ex.run("schema", {})
        rows = await self._ex.run(op, params)
        tap.event("graph.neo4j.exec", op=op, rows=len(rows))
        return rows

    # -- writes ----------------------------------------------------------------------------
    async def upsert_node(
        self,
        node_id: str,
        labels: Sequence[str] = (),
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None:
        ns = edge_namespace(namespace, properties)
        props = dict(properties or {})
        await self._run(
            "upsert_node",
            key=self.node_key(node_id, ns),
            id=node_id,
            ns=ns,
            labels=[str(x) for x in labels],
            props=_dump(props),
            compiled=str(props.get("compiled") or ""),
        )
        tap.event("graph.neo4j.merge_node", node=node_id, namespace=ns, labels=len(labels))

    async def upsert_edge(
        self,
        src: str,
        dst: str,
        rel_type: str,
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None:
        ns = edge_namespace(namespace, properties)
        props = dict(properties or {})
        await self._run(
            "upsert_edge",
            skey=self.node_key(src, ns),
            dkey=self.node_key(dst, ns),
            ekey=self.edge_key(src, dst, rel_type, ns),
            src=src,
            dst=dst,
            rel=rel_type,
            ns=ns,
            weight=edge_weight(props),
            kind=edge_kind(props) or "",
            props=_dump(props),
        )
        tap.event(
            "graph.neo4j.merge_edge",
            src=src,
            dst=dst,
            rel=rel_type,
            namespace=ns,
            weight=edge_weight(props),
        )

    async def delete_node(self, node_id: str) -> None:
        bare = [str(r["id"]) for r in await self._run("bare_neighbours", id=node_id)]
        await self._run("delete_node", id=node_id)
        if bare:
            await self._run("drop_bare", ids=bare)
        tap.event("graph.neo4j.delete_node", node=node_id)

    async def prune_dead(self, node_ids: Sequence[str], compiled: str) -> None:
        """Delete the compiled nodes among ``node_ids`` that no live edge touches any more."""
        ids = list(dict.fromkeys(node_ids))
        if ids and compiled:
            await self._run("prune_dead", ids=ids, compiled=compiled)
            tap.event("graph.neo4j.prune", nodes=len(ids))

    async def clear(self) -> None:
        await self._run("clear")
        tap.event("graph.neo4j.clear")

    # -- reads -----------------------------------------------------------------------------
    @staticmethod
    def _edge(row: Mapping[str, Any]) -> GraphEdge:
        return GraphEdge(str(row["src"]), str(row["dst"]), str(row["rel"]), _load(row.get("props")))

    async def edges_of(self, node_id: str) -> list[GraphEdge]:
        rows = await self._run("edges_of", ids=[node_id])
        return sorted((self._edge(r) for r in rows), key=lambda e: (e.src, e.dst, e.rel_type))

    async def edge_list(self, namespace: str | None = None) -> list[GraphEdge]:
        rows = await self._run("edge_list", ns=namespace)
        return sorted((self._edge(r) for r in rows), key=lambda e: (e.src, e.dst, e.rel_type))

    async def _walk(
        self,
        seeds: Sequence[str],
        depth: int,
        namespace: str | None,
        max_degree: int | None,
        rel_type: str | None = None,
    ) -> tuple[dict[str, int], list[GraphEdge]]:
        """Breadth-first over live edges, undirected, degree-capped (:func:`capped_neighbors`)."""
        dist = {s: 0 for s in seeds}
        frontier = list(dict.fromkeys(seeds))
        seen_edges: dict[tuple[str, str, str], GraphEdge] = {}
        for hop in range(1, max(depth, 0) + 1):
            if not frontier:
                break
            rows = await self._run("edges_of", ids=frontier)
            edges = [self._edge(r) for r in rows if namespace is None or r.get("ns") == namespace]
            nxt: list[str] = []
            for node in frontier:
                mine = [
                    e
                    for e in edges
                    if (rel_type is None or e.rel_type == rel_type) and node in (e.src, e.dst)
                ]
                for other in capped_neighbors(mine, node, max_degree):
                    if other not in dist:
                        dist[other] = hop
                        nxt.append(other)
                for e in mine:
                    if e.weight > 0:
                        seen_edges[(e.src, e.dst, e.rel_type)] = e
            frontier = nxt
        return dist, list(seen_edges.values())

    async def neighbors(
        self,
        node_id: str,
        rel_type: str | None = None,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphNode]:
        dist, _ = await self._walk([node_id], depth, namespace, max_degree, rel_type)
        found = [(d, n) for n, d in dist.items() if n != node_id]
        if not found:
            return []
        rows = await self._run("nodes", ids=[n for _, n in found])
        by_id = {str(r["id"]): r for r in rows}
        out = []
        for _, n in sorted(found):
            row = by_id.get(n) or {}
            labels = tuple(str(x) for x in row.get("labels") or ())
            out.append(GraphNode(n, labels, _load(row.get("props"))))
        return out

    async def subgraph(
        self,
        seeds: Sequence[str],
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphEdge]:
        dist, _ = await self._walk(list(seeds), depth, namespace, max_degree)
        if not dist:
            return []
        rows = await self._run("edges_of", ids=list(dist))
        keep = [self._edge(r) for r in rows if (namespace is None or r.get("ns") == namespace)]
        live = [e for e in keep if e.weight > 0 and e.src in dist and e.dst in dist]
        return sorted(live, key=lambda e: (e.src, e.dst, e.rel_type))

    async def node_count(self) -> int:
        rows = await self._run("node_count")
        return int(rows[0]["c"]) if rows else 0

    async def edge_count(self) -> int:
        rows = await self._run("edge_count")
        return int(rows[0]["c"]) if rows else 0

    async def close(self) -> None:
        await self._ex.close()
