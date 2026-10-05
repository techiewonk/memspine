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
    GraphEdge,
    GraphNode,
    edge_kind,
    edge_namespace,
    edge_weight,
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


def _walk_sql(namespace: str | None, rel_type: str | None, max_degree: int | None) -> str:
    """The recursive-CTE BFS: ``walk(node, depth)`` from the ``:seeds`` json array.

    ``UNION`` deduplicates ``(node, depth)`` rows, so the walk is bounded by
    ``nodes x depth``. A node re-expanded at a deeper level follows the same
    capped neighbour set (the cap is a property of the node, not the path), so
    the reachable set equals a level-by-level BFS with per-node caps.
    """
    where = ["weight > 0"]
    if namespace is not None:
        where.append("namespace = :namespace")
    if rel_type is not None:
        where.append("rel_type = :rel_type")
    live = " AND ".join(where)
    cap = ""
    if max_degree is not None:
        cap = (
            " AND a.other IN (SELECT b.other FROM adj b WHERE b.node = w.node"
            " GROUP BY b.other ORDER BY MAX(b.weight) DESC, b.other LIMIT :max_degree)"
        )
    return (
        "WITH RECURSIVE "
        f"adj(node, other, weight) AS ("
        f"SELECT src, dst, weight FROM graph_edges WHERE {live} AND src != dst "
        f"UNION ALL SELECT dst, src, weight FROM graph_edges WHERE {live} AND src != dst), "
        "walk(node, depth) AS ("
        "SELECT value, 0 FROM json_each(:seeds) "
        "UNION SELECT a.other, w.depth + 1 FROM walk w JOIN adj a ON a.node = w.node "
        f"WHERE w.depth < :depth{cap}) "
        "SELECT node, MIN(depth) AS hops FROM walk GROUP BY node"
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
    ) -> dict[str, int]:
        """node id -> hop count from the nearest seed (seeds at 0), one query."""
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
        sql = text(_walk_sql(namespace, rel_type, max_degree))
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(sql, params)).all()
        return {str(row[0]): int(row[1]) for row in rows}

    async def neighbors(
        self,
        node_id: str,
        rel_type: str | None = None,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphNode]:
        hops = await self._walk([node_id], depth, namespace, rel_type, max_degree)
        hops.pop(node_id, None)
        if not hops:
            return []
        stmt = select(graph_nodes.c.node_id, graph_nodes.c.labels, graph_nodes.c.properties).where(
            graph_nodes.c.node_id.in_(list(hops))
        )
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
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
        reached = list(await self._walk(seeds, depth, namespace, None, max_degree))
        if not reached:
            return []
        stmt = self._edge_select().where(
            graph_edges.c.src.in_(reached),
            graph_edges.c.dst.in_(reached),
            graph_edges.c.weight > 0,
        )
        if namespace is not None:
            stmt = stmt.where(graph_edges.c.namespace == namespace)
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(stmt)).all()
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
