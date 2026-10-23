"""Neo4j graph store (F-16e, M-04 / M-18, owner Q4): the server profile's graph backend.

The store is a ``GraphStore`` over a small executor port, so the Cypher lives in one table
(:data:`CYPHER`) and a test can stand in an in-memory MERGE recorder for the driver. It is only
ever a *projection*: nothing writes to it directly. Selected as ``storage.graph`` (the one
graph client, ADR-081 amendment 2026-10-23), the engine's graph projector feeds it, and every
mutation is an idempotent MERGE.

Model. One node label and one relationship type carry everything; the memory-level labels and
edge relation ride as properties (so no user text is ever spliced into Cypher):

``(:MemNode {key, id, namespace, labels, props, compiled, bare})``
``(:MemNode)-[:MemRel {key, rel_type, namespace, weight, kind, props}]->(:MemNode)``

Keys. Unscoped (today's keys, parity with ``sql_adjacency``): a node is keyed by its id and an
edge by ``(src, dst, rel_type)``. With ``isolation.scope_keys_v2`` (``scoped=True``) both include
the namespace, so two tenants never MERGE onto one node or edge. ``delete_node`` and ``edges_of``
take a bare node id (the port has no namespace there) and match on the ``id`` property.

Edges with weight ``<= 0`` are the ADR-015 tombstone: stored, never traversed. Constructing the
store without an executor raises the D-10 error naming the ``[neo4j]`` extra.

Walks (FU-GAPS). ``neighbors`` and ``subgraph`` are ONE variable-length Cypher query each (ops
``walk_nodes`` / ``walk_edges``: ``(s)-[:R*1..D]-(m)`` over live edges, the namespace and relation
filters inside the path predicate), not one round trip per hop. A walk with a ``max_degree`` cap
keeps the per-hop loop: the cap ranks each node's own neighbours (weight, then id), which a path
pattern cannot express.

Qualified walks (RM-94). ``neighbors`` / ``subgraph`` with a ``qualifier_filter`` run the shared
level-by-level walk (:func:`~memspine.services.graph.qualifiers.qualified_hops`): one
``frontier_edges`` query per hop returns the live edges touching the frontier inside the
namespace and relation filter, and the shared Python filter then drops edges the
:class:`~memspine.services.graph.qualifiers.QualifierFilter` does not admit before the fan-out
cap ranks what is left. So every provider answers one contract.

RM-96 pushdown. Each edge also carries its qualifiers as flat relationship properties
(:mod:`~memspine.services.graph.qualifier_columns`, ``SET r += $q`` on upsert; edges written
before RM-96 are backfilled once per store before its first pushed-down walk and never
traversed while stale). With an executor that runs Cypher (``compiles_qualifier_filters``),
a qualified walk of depth 1 or 2 is ONE ``qualified_walk`` query: the QualifierFilter
compiled into its WHERE clause, the shared walk's caps and ordering in Cypher (per-seed
``collect`` after ``ORDER BY weight DESC, id`` sliced to ``max_degree``; the level-2
frontier of the ``QUALIFIED_WALK_FRONTIER_CAP`` strongest level-1 nodes, each sliced to the
deep degree), plus the reached nodes' rows and, for ``subgraph``, the admitted induced
edges. Depth 0 or above 2, a cap below 1, an unknown filter field, and the in-memory test
recorder run the shared walk (the reference). Known divergence: Neo4j orders strings by
UTF-16 code unit, Python by code point, so a weight tie between two ids that differ only by
a supplementary-plane character versus U+E000..U+FFFF could break differently.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Protocol

from memspine.config.constants import (
    QUALIFIED_WALK_DEEP_MAX_DEGREE,
    QUALIFIED_WALK_FRONTIER_CAP,
)
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
from memspine.services.graph.qualifier_columns import (
    QUALIFIER_COLUMNS_VERSION,
    compile_filter,
    qualifier_columns,
)
from memspine.services.graph.qualifiers import (
    QualifierFilter,
    _deep_degree,
    qualified_hops,
    qualified_subgraph,
    sanitize_qualifiers,
)

if TYPE_CHECKING:
    from memspine.clients.neo4j import Neo4jClient

__all__ = [
    "CYPHER",
    "DriverExecutor",
    "Neo4jExecutor",
    "Neo4jGraphStore",
    "connector_executor",
]

_IDENT = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,40}$")
_SEP = "\x1f"
#: The deepest single-query walk; a deeper request is clamped (the loop it replaces had no cap).
MAX_WALK_DEPTH = 16
#: RM-96: edges re-derived per round trip by the qualifier-property backfill.
_BACKFILL_BATCH = 2000

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
        "r.props = $props SET r += $q"  # RM-96: the qualifier properties (null = removed)
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
    # FU-GAPS: the whole walk in one query. ``{D}`` is the validated depth (the executor formats it;
    # a variable-length bound cannot be a parameter). Shortest hop count per reached node id.
    "walk_nodes": (
        "MATCH (s:{N}) WHERE s.id IN $seeds "
        "MATCH p = (s)-[rels:{R}*1..{D}]-(m:{N}) "
        "WHERE all(r IN rels WHERE r.weight > 0 AND ($ns IS NULL OR r.namespace = $ns) "
        "AND ($rel IS NULL OR r.rel_type = $rel)) "
        "WITH m.id AS id, min(length(p)) AS dist WHERE NOT id IN $seeds "
        "MATCH (n:{N}) WHERE n.id = id "
        "RETURN id, dist, n.labels AS labels, n.props AS props"
    ),
    # The live edges between the seeds and everything within ``{D}`` hops of them.
    "walk_edges": (
        "MATCH (s:{N}) WHERE s.id IN $seeds "
        "OPTIONAL MATCH (s)-[rels:{R}*1..{D}]-(m:{N}) "
        "WHERE all(r IN rels WHERE r.weight > 0 AND ($ns IS NULL OR r.namespace = $ns)) "
        "WITH collect(DISTINCT s.id) + collect(DISTINCT m.id) AS reach "
        "MATCH (a:{N})-[r:{R}]->(b:{N}) "
        "WHERE a.id IN reach AND b.id IN reach AND r.weight > 0 "
        "AND ($ns IS NULL OR r.namespace = $ns) "
        "RETURN DISTINCT a.id AS src, b.id AS dst, r.rel_type AS rel, r.namespace AS ns, "
        "r.props AS props"
    ),
    # RM-94: one level of the shared qualified walk. Live edges touching the frontier inside
    # the namespace / relation filter; qualifier predicates are applied by the shared walk.
    # Anchored on the frontier nodes (expand from them, not a scan of every relationship).
    "frontier_edges": (
        "MATCH (n:{N}) WHERE n.id IN $ids "
        "MATCH (n)-[r:{R}]-(:{N}) WHERE r.weight > 0 AND ($ns IS NULL OR r.namespace = $ns) "
        "AND ($rel IS NULL OR r.rel_type = $rel) "
        "WITH DISTINCT r "
        "RETURN startNode(r).id AS src, endNode(r).id AS dst, r.rel_type AS rel, "
        "r.namespace AS ns, r.props AS props"
    ),
    # RM-96: the qualified walk (depth 1 or 2) in ONE query. ``{W}`` is the QualifierFilter
    # compiled over ``r`` by services/graph/qualifier_columns.py (parameter names only; the
    # executor formats it in, see DriverExecutor.run). Level 1: each seed's ``$deg1`` strongest
    # admitted neighbours (weight desc, then id); level 2: the ``$cap`` strongest level-1 nodes
    # (strength desc, then id) each follow their ``$deg2`` strongest admitted neighbours (the
    # shared qualified_hops rules; a null cap is no cap). Then the reached nodes' rows and, with
    # ``$edges``, the admitted induced edges. Aggregating subqueries always return one row.
    "qualified_walk": (
        "CALL () {{ "
        "UNWIND $seeds AS sid MATCH (s:{N}) WHERE s.id = sid "
        "MATCH (s)-[r:{R}]-(o:{N}) WHERE o.id <> sid AND r.weight > 0 "
        "AND ($ns IS NULL OR r.namespace = $ns) AND ($rel IS NULL OR r.rel_type = $rel) AND {W} "
        "WITH sid, o.id AS oid, max(r.weight) AS w ORDER BY w DESC, oid "
        "WITH sid, collect([oid, w]) AS ranked "
        "UNWIND CASE WHEN $deg1 IS NULL THEN ranked ELSE ranked[..$deg1] END AS p "
        "WITH p[0] AS oid, max(p[1]) AS strength WHERE NOT oid IN $seeds "
        "RETURN collect([oid, strength]) AS l1 }} "
        "CALL (l1) {{ "
        "UNWIND CASE WHEN $qdepth >= 2 THEN l1 ELSE [] END AS x "
        "WITH x ORDER BY x[1] DESC, x[0] WITH collect(x[0]) AS ordered "
        "UNWIND CASE WHEN $cap IS NULL THEN ordered ELSE ordered[..$cap] END AS fid "
        "MATCH (f:{N}) WHERE f.id = fid "
        "MATCH (f)-[r:{R}]-(o:{N}) WHERE o.id <> fid AND r.weight > 0 "
        "AND ($ns IS NULL OR r.namespace = $ns) AND ($rel IS NULL OR r.rel_type = $rel) AND {W} "
        "WITH fid, o.id AS oid, max(r.weight) AS w ORDER BY w DESC, oid "
        "WITH fid, collect(oid) AS ranked "
        "UNWIND CASE WHEN $deg2 IS NULL THEN ranked ELSE ranked[..$deg2] END AS pick "
        "RETURN collect(DISTINCT pick) AS l2 }} "
        "WITH [x IN l1 | x[0]] AS l1ids, l2 "
        "WITH l1ids, [y IN l2 WHERE NOT y IN l1ids AND NOT y IN $seeds] AS l2new "
        "CALL (l1ids, l2new) {{ "
        "UNWIND l1ids + l2new AS nid MATCH (n:{N}) WHERE n.id = nid "
        "RETURN collect({{id: n.id, labels: n.labels, props: n.props, ns: n.namespace}}) "
        "AS nodes }} "
        "CALL (l1ids, l2new) {{ "
        "WITH $seeds + l1ids + l2new AS reach WHERE $edges "
        "MATCH (a:{N})-[r:{R}]->(b:{N}) WHERE a.id IN reach AND b.id IN reach "
        "AND a.id <> b.id AND r.weight > 0 AND ($ns IS NULL OR r.namespace = $ns) AND {W} "
        "RETURN collect({{src: a.id, dst: b.id, rel: r.rel_type, ns: r.namespace, "
        "props: r.props}}) AS edges }} "
        "RETURN l1ids, l2new, nodes, edges"
    ),
    # RM-96: the walk looks nodes up by id per seed / frontier node / reached node, so it needs
    # an index on id (the key constraint indexes key only); created before the first pushdown.
    "id_index": "CREATE INDEX {N}_id IF NOT EXISTS FOR (n:{N}) ON (n.id)",
    "await_indexes": "CALL db.awaitIndexes(300)",
    # RM-96: edges whose qualifier properties are missing or of another version (backfill).
    "qualifier_stale": (
        "MATCH ()-[r:{R}]->() WHERE r.q_v IS NULL OR r.q_v <> $v "
        "RETURN elementId(r) AS eid, r.props AS props LIMIT $limit"
    ),
    "qualifier_set": (
        "UNWIND $rows AS x MATCH ()-[r:{R}]->() WHERE elementId(r) = x.eid SET r += x.q"
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
    """:class:`Neo4jExecutor` over a connector-owned async driver (``[neo4j]``).

    It borrows the driver: :meth:`close` never closes it (the connector's owner does).
    ``request_timeout_s`` bounds each operation (None = unbounded)."""

    def __init__(
        self,
        driver: Any,
        *,
        database: str = "neo4j",
        node_label: str = "MemNode",
        rel_type: str = "MemRel",
        request_timeout_s: float | None = None,
    ) -> None:
        self._driver = driver
        self._database = database
        self._timeout = request_timeout_s
        self._fmt = {"N": _check_ident(node_label, "label"), "R": _check_ident(rel_type, "type")}

    #: RM-96: this executor runs Cypher, so the store may send a compiled qualifier filter
    #: (the ``qualified_walk`` op). An executor without it (the in-memory test recorder)
    #: gets the shared level-by-level walk.
    compiles_qualifier_filters = True

    async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
        fmt = self._fmt
        if "depth" in params:  # a variable-length bound is text, validated here
            fmt = {**fmt, "D": max(1, min(int(params["depth"]), MAX_WALK_DEPTH))}  # type: ignore[dict-item]  # CI-GREEN: pre-existing typing debt
        sent = dict(params)
        if "_where" in sent:  # RM-96: compiled by qualifier_columns.compile_filter, params only
            fmt = {**fmt, "W": str(sent.pop("_where"))}
        text = CYPHER[op].format(**fmt)

        async def work(tx: Any) -> list[dict[str, Any]]:
            result = await tx.run(text, sent)
            return [dict(record) async for record in result]

        async with asyncio.timeout(self._timeout):
            async with self._driver.session(database=self._database) as session:
                return list(await session.execute_write(work))

    async def close(self) -> None:
        # The driver belongs to the connector; its owner closes it.
        return None


def connector_executor(
    connector: Neo4jClient, *, node_label: str = "MemNode", rel_type: str = "MemRel"
) -> DriverExecutor:
    """The live executor over an opened :class:`~memspine.clients.neo4j.Neo4jClient`
    (database and request deadline from its spec)."""
    spec = connector.spec
    return DriverExecutor(
        connector.handle,
        database=spec.database,
        node_label=node_label,
        rel_type=rel_type,
        request_timeout_s=spec.request_timeout_s,
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
        self._qualifier_columns_done = False  # RM-96 backfill ran (once per store)
        self._id_index_done = False  # RM-96 node id index ensured (once per store)

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
        props = sanitize_qualifiers(
            properties, src=src, dst=dst, rel_type=rel_type, provider="neo4j"
        )
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
            q=qualifier_columns(props),
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
        qualifier_filter: QualifierFilter | None = None,
    ) -> list[GraphNode]:
        if qualifier_filter is not None:
            return await self._neighbors_qualified(
                node_id, qualifier_filter, rel_type, depth, namespace, max_degree
            )
        if depth <= 0:
            return []
        if max_degree is None and depth <= MAX_WALK_DEPTH:  # FU-GAPS: one variable-length query
            rows = await self._run(
                "walk_nodes", seeds=[node_id], depth=depth, ns=namespace, rel=rel_type
            )
            hit = {str(r["id"]): r for r in rows}
            dists = {str(r["id"]): int(r["dist"]) for r in rows}
            out = []
            for n in sorted(dists, key=lambda i: (dists[i], i)):
                row = hit[n]
                labels = tuple(str(x) for x in row.get("labels") or ())
                out.append(GraphNode(n, labels, _load(row.get("props"))))
            return out
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
        qualifier_filter: QualifierFilter | None = None,
    ) -> list[GraphEdge]:
        if qualifier_filter is not None:  # RM-96: one query; RM-94: the shared walk otherwise
            walk = await self._qualified_walk(
                seeds, depth, qualifier_filter, namespace, None, max_degree, edges=True
            )
            if walk is not None:
                induced: dict[tuple[str, str, str], GraphEdge] = {}
                for row in walk["edges"]:
                    edge = self._edge(row)
                    induced.setdefault((edge.src, edge.dst, edge.rel_type), edge)
                return [induced[key] for key in sorted(induced)]

            async def frontier(nodes: Sequence[str]) -> list[GraphEdge]:
                return await self.frontier_edges(nodes, namespace=namespace)

            return await qualified_subgraph(frontier, seeds, depth, qualifier_filter, max_degree)
        if max_degree is None and seeds and 0 < depth <= MAX_WALK_DEPTH:  # FU-GAPS: one query
            rows = await self._run(
                "walk_edges", seeds=list(dict.fromkeys(seeds)), depth=depth, ns=namespace
            )
            edges = [self._edge(r) for r in rows if namespace is None or r.get("ns") == namespace]
            return sorted(edges, key=lambda e: (e.src, e.dst, e.rel_type))
        dist, _ = await self._walk(list(seeds), depth, namespace, max_degree)
        if not dist:
            return []
        rows = await self._run("edges_of", ids=list(dist))
        keep = [self._edge(r) for r in rows if (namespace is None or r.get("ns") == namespace)]
        live = [e for e in keep if e.weight > 0 and e.src in dist and e.dst in dist]
        return sorted(live, key=lambda e: (e.src, e.dst, e.rel_type))

    async def frontier_edges(
        self,
        nodes: Sequence[str],
        *,
        namespace: str | None = None,
        rel_type: str | None = None,
    ) -> list[GraphEdge]:
        """RM-94/RM-104: the live edges touching ``nodes`` (one ``frontier_edges`` op)."""
        if not nodes:
            return []
        await self._ensure_id_index()
        rows = await self._run(
            "frontier_edges", ids=list(dict.fromkeys(nodes)), ns=namespace, rel=rel_type
        )
        edges = [self._edge(r) for r in rows if namespace is None or r.get("ns") == namespace]
        return [e for e in edges if e.weight > 0 and (rel_type is None or e.rel_type == rel_type)]

    async def _neighbors_qualified(
        self,
        node_id: str,
        qualifier_filter: QualifierFilter,
        rel_type: str | None,
        depth: int,
        namespace: str | None,
        max_degree: int | None,
    ) -> list[GraphNode]:
        """RM-94: ``neighbors`` over the edges ``qualifier_filter`` admits only (RM-96: one
        ``qualified_walk`` query when the executor runs Cypher, else the shared walk)."""
        walk = await self._qualified_walk(
            [node_id], depth, qualifier_filter, namespace, rel_type, max_degree, edges=False
        )
        if walk is not None:
            hops = {str(n): 1 for n in walk["l1ids"]}
            hops.update({str(n): 2 for n in walk["l2new"]})
            return self._hop_nodes(hops, list(walk["nodes"]), namespace)

        async def frontier_edges(frontier: Sequence[str]) -> list[GraphEdge]:
            return await self.frontier_edges(frontier, namespace=namespace, rel_type=rel_type)

        hops = await qualified_hops(frontier_edges, [node_id], depth, qualifier_filter, max_degree)
        hops.pop(node_id, None)
        if not hops:
            return []
        return self._hop_nodes(hops, await self._run("nodes", ids=list(hops)), namespace)

    async def _qualified_walk(
        self,
        seeds: Sequence[str],
        depth: int,
        qualifier_filter: QualifierFilter,
        namespace: str | None,
        rel_type: str | None,
        max_degree: int | None,
        *,
        edges: bool,
    ) -> dict[str, Any] | None:
        """RM-96: the ``qualified_walk`` row, or None for the shared-walk fallback (an
        executor that does not run Cypher, depth outside 1..2, no seeds, a cap below 1, or a
        filter field the compiler does not know)."""
        if not getattr(self._ex, "compiles_qualifier_filters", False):
            return None
        deep = _deep_degree(max_degree, QUALIFIED_WALK_DEEP_MAX_DEGREE)
        caps = (max_degree, QUALIFIED_WALK_FRONTIER_CAP, deep)
        if depth not in (1, 2) or not seeds or any(c is not None and c < 1 for c in caps):
            return None
        params: dict[str, Any] = {}
        where = compile_filter(qualifier_filter, "r", params, prefix="qf")
        if where is None:
            return None
        await self._ensure_qualifier_columns()
        rows = await self._run(
            "qualified_walk",
            seeds=list(dict.fromkeys(seeds)),
            qdepth=depth,
            ns=namespace,
            rel=rel_type,
            deg1=max_degree,
            cap=QUALIFIED_WALK_FRONTIER_CAP,
            deg2=deep,
            edges=edges,
            _where=where,
            **params,
        )
        return rows[0]

    async def _ensure_qualifier_columns(self) -> None:
        """RM-96: once per store, before the first pushed-down walk: the node ``id`` index
        the walk's lookups need, then the qualifier properties of edges written without them
        (before RM-96) or with another version. Until then such an edge fails the ``q_v``
        check (never traversed, fail closed)."""
        if self._qualifier_columns_done:
            return
        await self._ensure_id_index()
        while rows := await self._run(
            "qualifier_stale", v=QUALIFIER_COLUMNS_VERSION, limit=_BACKFILL_BATCH
        ):
            await self._run(
                "qualifier_set",
                rows=[{"eid": r["eid"], "q": qualifier_columns(_load(r["props"]))} for r in rows],
            )
        self._qualifier_columns_done = True

    async def _ensure_id_index(self) -> None:
        """RM-96: the node ``id`` index (once per store) on an executor that runs Cypher; the
        walk and ``frontier_edges`` look nodes up by id (the key constraint indexes key)."""
        if self._id_index_done or not getattr(self._ex, "compiles_qualifier_filters", False):
            return
        await self._run("id_index")
        await self._run("await_indexes")
        self._id_index_done = True

    @staticmethod
    def _hop_nodes(
        hops: Mapping[str, int], rows: Sequence[Mapping[str, Any]], namespace: str | None
    ) -> list[GraphNode]:
        by_id: dict[str, Mapping[str, Any]] = {}
        for row in rows:  # unscoped ids are unique; scoped ones prefer the walked namespace
            rid = str(row["id"])
            if rid not in by_id or (namespace is not None and row.get("ns") == namespace):
                by_id[rid] = row
        out = []
        for n in sorted(hops, key=lambda i: (hops[i], i)):
            row = by_id.get(n) or {}
            labels = tuple(str(x) for x in row.get("labels") or ())
            out.append(GraphNode(n, labels, _load(row.get("props"))))
        return out

    async def node_count(self) -> int:
        rows = await self._run("node_count")
        return int(rows[0]["c"]) if rows else 0

    async def edge_count(self) -> int:
        rows = await self._run("edge_count")
        return int(rows[0]["c"]) if rows else 0

    async def close(self) -> None:
        await self._ex.close()
