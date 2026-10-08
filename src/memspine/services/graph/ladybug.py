"""LadybugDB graph store — the graph engine for graph features (ADR-034), ``[graph]``.

LadybugDB (PyPI ``ladybug``, import ``ladybug``) is the maintained fork of Kùzu
(Kùzu itself was archived on 2025-10-10). The fork kept Kùzu's embedded-Cypher
Python API and DDL dialect (``Database``/``Connection``, a synchronous client,
``CREATE NODE/REL TABLE IF NOT EXISTS``, ``ALTER TABLE ... ADD IF NOT EXISTS``,
recursive patterns ``-[*1..k]-``), pinned to ``ladybug>=0.21,<0.22``. The release
history this adapter was written against: 0.18.0 (2026-07-01, first PyPI
release) through 0.21.2 (2026-10-01).

``graph.provider`` still defaults to ``sqlite_adjacency`` (zero-dep, D-03 slim
core); ADR-034 records that LadybugDB is the engine graph features build on,
without flipping the default.

Schema: one node table (``MemoryNode``) and one rel table (``MemoryLink``),
created lazily on first use; labels/properties ride as canonical orjson strings
so the port's free-form payloads survive the fixed columnar schema. KB-1/KB-5:
``namespace`` is a property of both, and ``weight``/``kind`` are rel columns, so
the walk filters tenants and tombstones inside the engine. A database created
before these columns existed is altered in place and backfilled from the
properties blobs on first use.

Walks (KB-5): without a fan-out cap, ``neighbors`` is one native Cypher
variable-length query, ``-[e:MemoryLink* SHORTEST 1..k (r, _ | WHERE ...)]-``,
whose ``length(e)`` is the hop count. With ``max_degree`` the walk goes level by
level, one query per level, applying
:func:`~memspine.services.graph.base.capped_neighbors` (Cypher has no per-node
``LIMIT`` inside a recursive pattern).

The Python API is synchronous — every call is pushed to a worker thread, and a
single lock serializes them (one embedded connection is not a pool).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any, Protocol

import orjson

from memspine.services.graph.base import (
    GraphEdge,
    GraphNode,
    capped_neighbors,
    edge_kind,
    edge_namespace,
    edge_weight,
    entity_terms,
    fuse_entity_hits,
)

__all__ = ["CypherClient", "LadybugGraphStore"]


class CypherClient(Protocol):
    """What the adapter needs from its client: one embedded connection (D-24)."""

    @property
    def connection(self) -> Any: ...


_DDL = (
    "CREATE NODE TABLE IF NOT EXISTS MemoryNode("
    "node_id STRING, labels STRING, properties STRING, namespace STRING, kind STRING, "
    "PRIMARY KEY (node_id))",
    "CREATE REL TABLE IF NOT EXISTS MemoryLink("
    "FROM MemoryNode TO MemoryNode, rel_type STRING, properties STRING, "
    "namespace STRING, weight DOUBLE, kind STRING)",
)

#: In-place upgrade of a database created before KB-1 (no-ops when present).
_ALTER = (
    "ALTER TABLE MemoryNode ADD IF NOT EXISTS namespace STRING",
    "ALTER TABLE MemoryNode ADD IF NOT EXISTS kind STRING",
    "ALTER TABLE MemoryLink ADD IF NOT EXISTS namespace STRING",
    "ALTER TABLE MemoryLink ADD IF NOT EXISTS weight DOUBLE",
    "ALTER TABLE MemoryLink ADD IF NOT EXISTS kind STRING",
)

_UPSERT_NODE = (
    "MERGE (n:MemoryNode {node_id: $node_id}) "
    "ON CREATE SET n.labels = $labels, n.properties = $properties, "
    "n.namespace = $namespace, n.kind = $kind "
    "ON MATCH SET n.labels = $labels, n.properties = $properties, "
    "n.namespace = $namespace, n.kind = $kind"
)

_ENSURE_NODE = (
    "MERGE (n:MemoryNode {node_id: $node_id}) "
    "ON CREATE SET n.labels = '[]', n.properties = '{}', n.namespace = $namespace"
)

_UPSERT_EDGE = (
    "MATCH (a:MemoryNode {node_id: $src}), (b:MemoryNode {node_id: $dst}) "
    "MERGE (a)-[e:MemoryLink {rel_type: $rel_type}]->(b) "
    "ON CREATE SET e.properties = $properties, e.namespace = $namespace, "
    "e.weight = $weight, e.kind = $kind "
    "ON MATCH SET e.properties = $properties, e.namespace = $namespace, "
    "e.weight = $weight, e.kind = $kind"
)

_EDGE_MATCH = "MATCH (s:MemoryNode)-[e:MemoryLink]->(d:MemoryNode)"
_EDGE_RETURN = "RETURN s.node_id, d.node_id, e.rel_type, e.properties"
_NODE_RETURN = "b.node_id, b.labels, b.properties"


def _loads(raw: object) -> Any:
    return orjson.loads(raw if isinstance(raw, str | bytes) else "null")


def _edge(row: list[Any]) -> GraphEdge:
    return GraphEdge(
        src=str(row[0]),
        dst=str(row[1]),
        rel_type=str(row[2]),
        properties=dict(_loads(row[3]) or {}),
    )


def _node(row: list[Any]) -> GraphNode:
    return GraphNode(
        node_id=str(row[0]),
        labels=tuple(_loads(row[1]) or ()),
        properties=dict(_loads(row[2]) or {}),
    )


def _filters(
    var: str, namespace: str | None, rel_type: str | None, params: dict[str, Any]
) -> list[str]:
    """Live-edge predicates on rel variable ``var`` (tombstones are gone, ADR-015)."""
    clauses = [f"{var}.weight > 0"]
    if namespace is not None:
        clauses.append(f"{var}.namespace = $namespace")
        params["namespace"] = namespace
    if rel_type is not None:
        clauses.append(f"{var}.rel_type = $rel_type")
        params["rel_type"] = rel_type
    return clauses


class LadybugGraphStore:
    """GraphStore over an embedded LadybugDB (or, deprecated, Kùzu) connection."""

    def __init__(self, client: CypherClient) -> None:
        self._client = client
        self._ready = False
        self._entity_dim: int | None = None  # GR-3: EntityVec table created
        self._lock = asyncio.Lock()

    async def _execute(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[list[Any]]:
        async with self._lock:
            return await self._execute_unlocked(query, parameters)

    async def _execute_unlocked(
        self, query: str, parameters: dict[str, Any] | None = None
    ) -> list[list[Any]]:
        """The raw statement runner — caller MUST hold ``self._lock`` (one
        embedded connection is not a pool). Exists so multi-statement units
        like ``upsert_edge`` stay atomic under a single lock acquisition."""
        if not self._ready:
            await asyncio.to_thread(self._prepare)
            self._ready = True

        def _run() -> list[list[Any]]:
            return self._rows(query, parameters or {})

        return await asyncio.to_thread(_run)

    def _rows(self, query: str, parameters: dict[str, Any]) -> list[list[Any]]:
        result = self._client.connection.execute(query, parameters=parameters)
        rows: list[list[Any]] = []
        while result.has_next():
            rows.append(list(result.get_next()))
        return rows

    def _prepare(self) -> None:
        """DDL, the KB-1 in-place upgrade, and its one-time backfill."""
        for statement in (*_DDL, *_ALTER):
            self._rows(statement, {})
        stale_nodes = self._rows(
            "MATCH (n:MemoryNode) WHERE n.namespace IS NULL "
            "RETURN n.node_id, n.labels, n.properties",
            {},
        )
        for node_id, labels, properties in stale_nodes:
            label_list = _loads(labels) or []
            self._rows(
                "MATCH (n:MemoryNode {node_id: $node_id}) SET n.namespace = $ns, n.kind = $kind",
                {
                    "node_id": node_id,
                    "ns": edge_namespace(None, dict(_loads(properties) or {})),
                    "kind": label_list[0] if label_list else None,
                },
            )
        stale_edges = self._rows(
            f"{_EDGE_MATCH} WHERE e.weight IS NULL "
            "RETURN s.node_id, d.node_id, e.rel_type, e.properties, s.namespace, d.namespace",
            {},
        )
        for src, dst, rel_type, properties, src_ns, dst_ns in stale_edges:
            props = dict(_loads(properties) or {})
            self._rows(
                "MATCH (s:MemoryNode {node_id: $src})-[e:MemoryLink {rel_type: $rel}]->"
                "(d:MemoryNode {node_id: $dst}) "
                "SET e.weight = $weight, e.kind = $kind, e.namespace = $ns",
                {
                    "src": src,
                    "dst": dst,
                    "rel": rel_type,
                    "weight": edge_weight(props),
                    "kind": edge_kind(props),
                    "ns": src_ns or dst_ns or "",
                },
            )

    async def upsert_node(
        self,
        node_id: str,
        labels: Sequence[str] = (),
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None:
        await self._execute(
            _UPSERT_NODE,
            {
                "node_id": node_id,
                "labels": orjson.dumps(list(labels)).decode(),
                "properties": orjson.dumps(dict(properties or {})).decode(),
                "namespace": edge_namespace(namespace, properties),
                "kind": labels[0] if labels else None,
            },
        )

    async def upsert_edge(
        self,
        src: str,
        dst: str,
        rel_type: str,
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None:
        props = dict(properties or {})
        ns = edge_namespace(namespace, None)
        # Endpoints are implicitly created bare (port contract): link replay
        # must never depend on node-event ordering. One lock acquisition spans
        # all three statements so a concurrent caller cannot interleave
        # between the ensure-node and upsert-edge steps.
        async with self._lock:
            for endpoint in (src, dst):
                await self._execute_unlocked(_ENSURE_NODE, {"node_id": endpoint, "namespace": ns})
            await self._execute_unlocked(
                _UPSERT_EDGE,
                {
                    "src": src,
                    "dst": dst,
                    "rel_type": rel_type,
                    "properties": orjson.dumps(props).decode(),
                    "namespace": ns,
                    "weight": edge_weight(props),
                    "kind": edge_kind(props),
                },
            )

    async def _hops(
        self,
        seeds: Sequence[str],
        depth: int,
        namespace: str | None,
        rel_type: str | None,
        max_degree: int | None,
    ) -> dict[str, int]:
        """node id -> hop count from the nearest seed (seeds at 0)."""
        hops = {seed: 0 for seed in seeds}
        if depth <= 0 or not seeds:
            return hops
        if max_degree is None:
            # Native variable-length BFS: one query for the whole walk.
            params: dict[str, Any] = {"seeds": list(seeds)}
            where = " AND ".join(_filters("r", namespace, rel_type, params))
            rows = await self._execute(
                "MATCH (a:MemoryNode)-[e:MemoryLink* SHORTEST 1.."
                f"{int(depth)} (r, _ | WHERE {where})]-(b:MemoryNode) "
                "WHERE a.node_id IN $seeds AND b.node_id <> a.node_id "
                "RETURN b.node_id, min(length(e))",
                params,
            )
            for node_id, hop in rows:
                hops.setdefault(str(node_id), int(hop))
                hops[str(node_id)] = min(hops[str(node_id)], int(hop))
            return hops
        # Capped walk: one query per level, the shared per-node cap rule.
        frontier = list(seeds)
        for level in range(1, depth + 1):
            params = {"frontier": frontier}
            where = " AND ".join(_filters("e", namespace, rel_type, params))
            rows = await self._execute(
                f"{_EDGE_MATCH} WHERE (s.node_id IN $frontier OR d.node_id IN $frontier) "
                f"AND {where} {_EDGE_RETURN}",
                params,
            )
            edges = [_edge(row) for row in rows]
            next_frontier: list[str] = []
            for node_id in frontier:
                for other in capped_neighbors(edges, node_id, max_degree):
                    if other not in hops:
                        hops[other] = level
                        next_frontier.append(other)
            if not next_frontier:
                break
            frontier = next_frontier
        return hops

    async def neighbors(
        self,
        node_id: str,
        rel_type: str | None = None,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphNode]:
        hops = await self._hops([node_id], depth, namespace, rel_type, max_degree)
        hops.pop(node_id, None)
        if not hops:
            return []
        rows = await self._execute(
            f"MATCH (b:MemoryNode) WHERE b.node_id IN $ids RETURN {_NODE_RETURN}",
            {"ids": list(hops)},
        )
        nodes = [_node(row) for row in rows]
        return sorted(nodes, key=lambda node: (hops[node.node_id], node.node_id))

    async def subgraph(
        self,
        seeds: Sequence[str],
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphEdge]:
        reached = list(await self._hops(seeds, depth, namespace, None, max_degree))
        if not reached:
            return []
        params: dict[str, Any] = {"ids": reached}
        where = " AND ".join(_filters("e", namespace, None, params))
        rows = await self._execute(
            f"{_EDGE_MATCH} WHERE s.node_id IN $ids AND d.node_id IN $ids AND {where} "
            f"{_EDGE_RETURN}",
            params,
        )
        return sorted((_edge(row) for row in rows), key=lambda e: (e.src, e.dst, e.rel_type))

    async def edges_of(self, node_id: str) -> list[GraphEdge]:
        rows = await self._execute(
            f"{_EDGE_MATCH} WHERE s.node_id = $node_id OR d.node_id = $node_id {_EDGE_RETURN}",
            {"node_id": node_id},
        )
        return [_edge(row) for row in rows]

    async def delete_node(self, node_id: str) -> None:
        await self._execute(
            "MATCH (n:MemoryNode {node_id: $node_id}) DETACH DELETE n", {"node_id": node_id}
        )
        if self._entity_dim is not None:  # GR-3: the entity's vector row goes too
            await self._execute(
                "MATCH (v:EntityVec {node_id: $node_id}) DELETE v", {"node_id": node_id}
            )

    async def _ensure_entity_table(self, dim: int) -> None:
        """GR-3/GR-5: the ``EntityVec`` table (node id, namespace, text, FLOAT[dim])
        with LadybugDB's native FTS index, created once."""
        if self._entity_dim is not None:
            if dim != self._entity_dim:
                raise ValueError(f"entity vector dim {dim} != table dim {self._entity_dim}")
            return
        for ext in ("FTS",):
            await self._execute(f"LOAD {ext}", {})
        await self._execute(
            "CREATE NODE TABLE IF NOT EXISTS EntityVec(node_id STRING, namespace STRING, "
            f"text STRING, emb FLOAT[{int(dim)}], PRIMARY KEY (node_id))",
            {},
        )
        # No HNSW vector index: LadybugDB 0.21.2's ``CREATE_VECTOR_INDEX`` crashes the
        # process at random on Windows (2 of 5 probe runs, 2026-10-08). The entity
        # search ranks by exact ``array_cosine_similarity`` inside the namespace
        # instead (stable, exact; entity counts per namespace are small).
        for ddl in ("CALL CREATE_FTS_INDEX('EntityVec', 'entity_fts_idx', ['text'])",):
            try:
                await self._execute(ddl, {})
            except Exception as exc:  # already present on a reopened database
                if "already exists" not in str(exc).lower():
                    raise
        self._entity_dim = dim

    async def _discover_entity_table(self) -> bool:
        """GR-3: a reopened database already holding ``EntityVec`` (dim from a row)."""
        try:
            for ext in ("FTS",):
                await self._execute(f"LOAD {ext}", {})
            rows = await self._execute("MATCH (v:EntityVec) RETURN size(v.emb) LIMIT 1", {})
        except Exception:
            return False
        if not rows:
            return False
        self._entity_dim = int(rows[0][0])
        return True

    async def upsert_entity_vector(
        self, namespace: str, node_id: str, text: str, vector: Sequence[float]
    ) -> None:
        """GR-3: an entity node's embedding and text in ``EntityVec`` (indexed)."""
        await self._ensure_entity_table(len(vector))
        # Insert once, never update in place: LadybugDB 0.21 crashes the process on
        # an update of a row covered by an FTS index (probed 2026-10-08). An entity
        # node id derives from its canonical name, so its text and vector never change.
        found = await self._execute(
            "MATCH (v:EntityVec {node_id: $node_id}) RETURN count(*)", {"node_id": node_id}
        )
        if found and int(found[0][0]) > 0:
            return
        await self._execute(
            "CREATE (:EntityVec {node_id: $node_id, namespace: $namespace, text: $text, "
            "emb: $emb})",
            {
                "node_id": node_id,
                "namespace": namespace,
                "text": text,
                "emb": [float(x) for x in vector],
            },
        )

    async def search_entities(
        self,
        namespace: str,
        vector: Sequence[float] | None,
        text: str | None,
        top_k: int = 8,
    ) -> list[tuple[str, float]]:
        """GR-6: entity nodes of one namespace by exact in-engine cosine and the FTS
        index (both filtered to the namespace inside the query), fused by RRF."""
        if self._entity_dim is None and not await self._discover_entity_table():
            return []
        pool = top_k * 4
        by_vector: list[str] = []
        if vector is not None:
            rows = await self._execute(
                "MATCH (v:EntityVec) WHERE v.namespace = $namespace "
                "RETURN v.node_id, array_cosine_similarity(v.emb, $emb) AS s "
                "ORDER BY s DESC, v.node_id LIMIT $k",
                {"emb": [float(x) for x in vector], "k": pool, "namespace": namespace},
            )
            by_vector = [str(r[0]) for r in rows][:pool]
        by_text: list[str] = []
        terms = " ".join(sorted(entity_terms(text or "")))
        if terms and "'" not in namespace:
            # Literals only: LadybugDB 0.21.2 crashes the process when a
            # ``QUERY_FTS_INDEX`` statement carries any parameter (probed 2026-10-08).
            # The text is reduced to [a-z0-9] word tokens; the namespace grammar
            # (``core.namespace``) admits no quotes.
            rows = await self._execute(
                f"CALL QUERY_FTS_INDEX('EntityVec', 'entity_fts_idx', '{terms}') "
                f"WITH node, score WHERE node.namespace = '{namespace}' "
                "RETURN node.node_id ORDER BY score DESC",
                {},
            )
            by_text = [str(r[0]) for r in rows][:pool]
        return fuse_entity_hits(by_vector, by_text, top_k)

    async def edge_list(self, namespace: str | None = None) -> list[GraphEdge]:
        if namespace is None:
            rows = await self._execute(f"{_EDGE_MATCH} {_EDGE_RETURN}")
        else:
            rows = await self._execute(
                f"{_EDGE_MATCH} WHERE e.namespace = $namespace {_EDGE_RETURN}",
                {"namespace": namespace},
            )
        return [_edge(row) for row in rows]

    async def node_count(self) -> int:
        rows = await self._execute("MATCH (n:MemoryNode) RETURN count(n)")
        return int(rows[0][0])

    async def edge_count(self) -> int:
        rows = await self._execute("MATCH ()-[e:MemoryLink]->() RETURN count(e)")
        return int(rows[0][0])

    async def clear(self) -> None:
        await self._execute("MATCH (n:MemoryNode) DETACH DELETE n")
        if self._entity_dim is not None or await self._discover_entity_table():
            await self._execute("MATCH (v:EntityVec) DELETE v")  # GR-3: rebuilt with the graph

    async def close(self) -> None:
        """No-op: the injected client owns the database handle (D-24)."""
