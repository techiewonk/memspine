"""Zero-dep graph fallback: adjacency lists in SQLite — the DEFAULT provider (D-26).

LadybugDB is the graph engine for graph features (ADR-034); this adapter keeps
shallow associative graphs on the same SQLite database as everything else, with
no extra install (D-03 slim core). Like every derived store it is a rebuildable
projection (D0.1): ``clear()`` + replay reproduces it from ``memory_events``.

KB-2: a multi-hop walk is ONE recursive-CTE query (not a query per node). The
CTE walks an undirected view of the live edges (``weight > 0``) of one
namespace; with ``max_degree`` each step follows only the node's strongest
neighbours (a correlated ``ORDER BY weight DESC, other LIMIT k``, served by the
``(namespace, src|dst, weight)`` indexes), which is exactly the rule of
:func:`~memspine.services.graph.base.capped_neighbors`.

Consumes an injected :class:`SQLiteClient` (D-22/D-24) — this service never
opens a connection itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import orjson
from sqlalchemy import Select, delete, func, or_, select, text

from memspine.clients.sqlite import SQLiteClient
from memspine.services.graph.base import (
    ENTITY_TEXT_PROP,
    ENTITY_VECTOR_PROP,
    GraphEdge,
    GraphNode,
    cosine,
    edge_kind,
    edge_namespace,
    edge_weight,
    entity_terms,
    fuse_entity_hits,
)
from memspine.services.storage.sqlite.schema import graph_edges, graph_nodes

__all__ = ["SQLiteAdjacencyGraph"]

_EDGE_KEY = ["src", "dst", "rel_type"]


def _props(raw: bytes) -> dict[str, object]:
    loaded: dict[str, object] = orjson.loads(raw)
    return loaded


# Rows and statements are typed loosely: SQLAlchemy 2.0 and 2.1 spell their generics
# differently (tuple[...] vs variadic), and both are supported (``sqlalchemy>=2.0``).
def _node(row: Any) -> GraphNode:
    return GraphNode(node_id=row[0], labels=tuple(orjson.loads(row[1])), properties=_props(row[2]))


def _edge(row: Any) -> GraphEdge:
    return GraphEdge(src=row[0], dst=row[1], rel_type=row[2], properties=_props(row[3]))


def _live(alias: str, namespace: str | None, rel_type: str | None) -> str:
    """The live-edge predicate on one ``graph_edges`` alias (``weight > 0``, no
    self-loops, optionally one namespace and one relation)."""
    where = [f"{alias}.weight > 0", f"{alias}.src != {alias}.dst"]
    if namespace is not None:
        where.append(f"{alias}.namespace = :namespace")
    if rel_type is not None:
        where.append(f"{alias}.rel_type = :rel_type")
    return " AND ".join(where)


def _walk_sql(namespace: str | None, rel_type: str | None, max_degree: int | None) -> str:
    """The recursive-CTE BFS prefix: ``walk(node, depth)`` from the ``:seeds`` json
    array and ``hops(node, hops)``, the hop count from the nearest seed; callers
    append the final ``SELECT``.

    The undirected step is two recursive selects, one per edge direction, each
    joining ``graph_edges`` on the frontier node so every step is an index seek
    on ``(namespace, src|dst, weight)`` (the primary key / ``dst`` index when no
    namespace is given). An earlier form joined a ``UNION ALL`` view of both
    directions, which SQLite materialised by scanning every edge on each step
    (~1 s per call at 100K edges, #24).

    With ``max_degree`` each node expands to its strongest neighbours only: the
    correlated scalar subquery ranks the node's incident live edges (both
    directions, ``MAX(weight) DESC, other``) and ``json_each`` unpacks the top
    ``k`` -- exactly the rule of :func:`~memspine.services.graph.base.capped_neighbors`.

    ``UNION`` deduplicates ``(node, depth)`` rows, so the walk is bounded by
    ``nodes x depth``. A node re-expanded at a deeper level follows the same
    capped neighbour set (the cap is a property of the node, not the path), so
    the reachable set equals a level-by-level BFS with per-node caps.
    """
    if max_degree is not None:
        ranked = (
            "SELECT json_group_array(other) FROM ("
            "SELECT other FROM ("
            f"SELECT c.dst AS other, c.weight AS weight FROM graph_edges c "
            f"WHERE c.src = w.node AND {_live('c', namespace, rel_type)} "
            f"UNION ALL SELECT c.src, c.weight FROM graph_edges c "
            f"WHERE c.dst = w.node AND {_live('c', namespace, rel_type)}) "
            "GROUP BY other ORDER BY MAX(weight) DESC, other LIMIT :max_degree)"
        )
        steps = (
            f"SELECT j.value, w.depth + 1 FROM walk w, json_each(({ranked})) j "
            "WHERE w.depth < :depth"
        )
    else:
        # CROSS JOIN pins the frontier as the outer loop: without ANALYZE
        # statistics the planner may otherwise scan the namespace's edges.
        steps = " UNION ".join(
            f"SELECT e.{far}, w.depth + 1 FROM walk w CROSS JOIN graph_edges e "
            f"ON e.{near} = w.node WHERE w.depth < :depth AND {_live('e', namespace, rel_type)}"
            for near, far in (("src", "dst"), ("dst", "src"))
        )
    return (
        "WITH RECURSIVE "
        "walk(node, depth) AS ("
        f"SELECT value, 0 FROM json_each(:seeds) UNION {steps}), "
        "hops(node, hops) AS (SELECT node, MIN(depth) FROM walk GROUP BY node) "
    )


#: The walk's node rows joined in SQL (never an ``IN (...)`` list of bound ids,
#: which overflows SQLite's variable limit on a large walk).
_NEIGHBOR_ROWS = (
    "SELECT n.node_id, n.labels, n.properties, h.hops FROM hops h "
    "JOIN graph_nodes n ON n.node_id = h.node"
)


def _subgraph_rows(namespace: str | None) -> str:
    """Every live edge with both endpoints in the walk, joined against it in SQL."""
    scope = " AND e.namespace = :namespace" if namespace is not None else ""
    return (
        "SELECT e.src, e.dst, e.rel_type, e.properties FROM graph_edges e "
        "WHERE e.weight > 0"
        f"{scope} AND e.src IN (SELECT node FROM hops) AND e.dst IN (SELECT node FROM hops)"
    )


class SQLiteAdjacencyGraph:
    def __init__(self, client: SQLiteClient) -> None:
        self._client = client

    async def upsert_node(
        self,
        node_id: str,
        labels: Sequence[str] = (),
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        values = {
            "labels": orjson.dumps(list(labels)),
            "properties": orjson.dumps(dict(properties or {})),
            "namespace": edge_namespace(namespace, properties),
            "kind": labels[0] if labels else None,
        }
        stmt = sqlite_insert(graph_nodes).values(node_id=node_id, **values)
        stmt = stmt.on_conflict_do_update(index_elements=["node_id"], set_=values)
        async with self._client.engine.begin() as conn:
            await conn.execute(stmt)

    async def upsert_edge(
        self,
        src: str,
        dst: str,
        rel_type: str,
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        props = dict(properties or {})
        ns = edge_namespace(namespace, None)
        empty = {"labels": orjson.dumps([]), "properties": orjson.dumps({}), "namespace": ns}
        values = {
            "properties": orjson.dumps(props),
            "namespace": ns,
            "weight": edge_weight(props),
            "kind": edge_kind(props),
        }
        edge_stmt = sqlite_insert(graph_edges).values(src=src, dst=dst, rel_type=rel_type, **values)
        edge_stmt = edge_stmt.on_conflict_do_update(index_elements=_EDGE_KEY, set_=values)
        async with self._client.engine.begin() as conn:
            # Endpoints are implicitly created bare (port contract): link
            # replay must never depend on node-event ordering. do_nothing so
            # an existing node's labels/properties are untouched.
            for endpoint in (src, dst):
                await conn.execute(
                    sqlite_insert(graph_nodes)
                    .values(node_id=endpoint, **empty)
                    .on_conflict_do_nothing(index_elements=["node_id"])
                )
            await conn.execute(edge_stmt)

    async def _walk(
        self,
        seeds: Sequence[str],
        depth: int,
        namespace: str | None,
        rel_type: str | None,
        max_degree: int | None,
        select: str = "SELECT node, hops FROM hops",
    ) -> list[Any]:
        """The walk's rows under ``select`` (seeds at hop 0), one query."""
        params: dict[str, object] = {
            "seeds": orjson.dumps(list(seeds)).decode(),
            "depth": max(depth, 0),
        }
        if namespace is not None:
            params["namespace"] = namespace
        if rel_type is not None:
            params["rel_type"] = rel_type
        if max_degree is not None:
            params["max_degree"] = max(max_degree, 0)
        sql = text(_walk_sql(namespace, rel_type, max_degree) + select)
        async with self._client.engine.connect() as conn:
            return list((await conn.execute(sql, params)).all())

    async def neighbors(
        self,
        node_id: str,
        rel_type: str | None = None,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphNode]:
        rows = await self._walk(
            [node_id], depth, namespace, rel_type, max_degree, select=_NEIGHBOR_ROWS
        )
        found = [(int(row[3]), _node(row)) for row in rows if row[0] != node_id]
        return [node for _, node in sorted(found, key=lambda pair: (pair[0], pair[1].node_id))]

    async def subgraph(
        self,
        seeds: Sequence[str],
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphEdge]:
        rows = await self._walk(
            seeds, depth, namespace, None, max_degree, select=_subgraph_rows(namespace)
        )
        return sorted((_edge(row) for row in rows), key=lambda e: (e.src, e.dst, e.rel_type))

    async def edges_of(self, node_id: str) -> list[GraphEdge]:
        stmt = self._edge_select().where(
            or_(graph_edges.c.src == node_id, graph_edges.c.dst == node_id)
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_edge(row) for row in rows]

    async def delete_node(self, node_id: str) -> None:
        async with self._client.engine.begin() as conn:
            # Cascade both directions (M7): a forgotten memory must stop being
            # reachable from every neighbour, not just its own out-links.
            await conn.execute(
                delete(graph_edges).where(
                    or_(graph_edges.c.src == node_id, graph_edges.c.dst == node_id)
                )
            )
            await conn.execute(delete(graph_nodes).where(graph_nodes.c.node_id == node_id))

    async def edge_list(self, namespace: str | None = None) -> list[GraphEdge]:
        stmt = self._edge_select()
        if namespace is not None:
            stmt = stmt.where(graph_edges.c.namespace == namespace)
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
        return [_edge(row) for row in rows]

    async def upsert_entity_vector(
        self, namespace: str, node_id: str, text: str, vector: Sequence[float]
    ) -> None:
        """GR-3: store an entity node's embedding and text in its properties (the
        node is created bare when absent, like an edge endpoint)."""
        async with self._client.engine.begin() as conn:
            row = (
                await conn.execute(
                    select(graph_nodes.c.labels, graph_nodes.c.properties).where(
                        graph_nodes.c.node_id == node_id
                    )
                )
            ).first()
        labels = list(orjson.loads(row[0])) if row is not None else []
        props = _props(row[1]) if row is not None else {}
        props[ENTITY_VECTOR_PROP] = [float(x) for x in vector]
        props[ENTITY_TEXT_PROP] = text
        await self.upsert_node(node_id, labels, props, namespace=namespace)

    async def search_entities(
        self,
        namespace: str,
        vector: Sequence[float] | None,
        text: str | None,
        top_k: int = 8,
    ) -> list[tuple[str, float]]:
        """GR-6: entity nodes of one namespace ranked by cosine (vector) and word overlap
        (text), fused by RRF. Computed in Python over the namespace's embedded nodes."""
        async with self._client.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(graph_nodes.c.node_id, graph_nodes.c.properties).where(
                        graph_nodes.c.namespace == namespace
                    )
                )
            ).all()
        nodes = [(r[0], _props(r[1])) for r in rows]
        nodes = [(nid, p) for nid, p in nodes if ENTITY_VECTOR_PROP in p]
        by_vector: list[str] = []
        if vector is not None:
            scored = [
                (cosine(vector, p[ENTITY_VECTOR_PROP]), nid)  # type: ignore[arg-type]
                for nid, p in nodes
            ]
            by_vector = [nid for _, nid in sorted(scored, key=lambda t: (-t[0], t[1]))]
        by_text: list[str] = []
        if text:
            asked = entity_terms(text)
            overlap = [
                (len(asked & entity_terms(str(p.get(ENTITY_TEXT_PROP, "")))), nid)
                for nid, p in nodes
            ]
            by_text = [nid for n, nid in sorted(overlap, key=lambda t: (-t[0], t[1])) if n > 0]
        return fuse_entity_hits(by_vector[: top_k * 4], by_text[: top_k * 4], top_k)

    async def node_count(self) -> int:
        return await self._count(select(func.count()).select_from(graph_nodes))

    async def edge_count(self) -> int:
        return await self._count(select(func.count()).select_from(graph_edges))

    async def clear(self) -> None:
        async with self._client.engine.begin() as conn:
            await conn.execute(delete(graph_edges))
            await conn.execute(delete(graph_nodes))

    async def close(self) -> None:
        """No-op: the injected SQLiteClient owns the connection (D-24)."""

    @staticmethod
    def _edge_select() -> Any:
        return select(
            graph_edges.c.src,
            graph_edges.c.dst,
            graph_edges.c.rel_type,
            graph_edges.c.properties,
        )

    async def _count(self, stmt: Select[Any]) -> int:
        async with self._client.engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())
