"""Zero-dep graph fallback: adjacency lists in SQLite — the v0.1 DEFAULT (D-26).

ladybugdb (the intended embedded default) is not on PyPI yet, so shallow
associative graphs run on the same SQLite database as everything else. Like
every derived store this is a rebuildable projection (D0.1): ``clear()`` +
replay reproduces it from ``memory_events``.

Consumes an injected :class:`SQLiteClient` (D-22/D-24) — this service never
opens a connection itself.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

import orjson
from sqlalchemy import Row, Select, delete, func, or_, select

from memspine.clients.sqlite import SQLiteClient
from memspine.services.graph.base import GraphEdge, GraphNode, walk_neighbors
from memspine.services.storage.sqlite.schema import graph_edges, graph_nodes

__all__ = ["SQLiteAdjacencyGraph"]

_EDGE_KEY = ["src", "dst", "rel_type"]


def _props(raw: bytes) -> dict[str, object]:
    loaded: dict[str, object] = orjson.loads(raw)
    return loaded


def _node(row: Row[tuple[str, bytes, bytes]]) -> GraphNode:
    return GraphNode(node_id=row[0], labels=tuple(orjson.loads(row[1])), properties=_props(row[2]))


def _edge(row: Row[tuple[str, str, str, bytes]]) -> GraphEdge:
    return GraphEdge(src=row[0], dst=row[1], rel_type=row[2], properties=_props(row[3]))


class SQLiteAdjacencyGraph:
    def __init__(self, client: SQLiteClient) -> None:
        self._client = client

    async def upsert_node(
        self,
        node_id: str,
        labels: Sequence[str] = (),
        properties: Mapping[str, object] | None = None,
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        values = {
            "labels": orjson.dumps(list(labels)),
            "properties": orjson.dumps(dict(properties or {})),
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
    ) -> None:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        empty = {"labels": orjson.dumps([]), "properties": orjson.dumps({})}
        values = {"properties": orjson.dumps(dict(properties or {}))}
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

    async def neighbors(
        self, node_id: str, rel_type: str | None = None, depth: int = 1
    ) -> list[GraphNode]:
        return await walk_neighbors(self._one_hop, node_id, rel_type, depth)

    async def _one_hop(self, node_id: str, rel_type: str | None) -> list[GraphNode]:
        edge_stmt = self._edge_select().where(
            or_(graph_edges.c.src == node_id, graph_edges.c.dst == node_id)
        )
        if rel_type is not None:
            edge_stmt = edge_stmt.where(graph_edges.c.rel_type == rel_type)
        async with self._client.engine.connect() as conn:
            edges = [_edge(row) for row in (await conn.execute(edge_stmt)).all()]
            # Tombstoned edges (weight <= 0, ADR-015) are gone for every
            # reader — a pruned neighbour must not resurface via a walk.
            adjacent = {
                edge.dst if edge.src == node_id else edge.src for edge in edges if edge.weight > 0.0
            }
            if not adjacent:
                return []
            stmt = select(
                graph_nodes.c.node_id, graph_nodes.c.labels, graph_nodes.c.properties
            ).where(graph_nodes.c.node_id.in_(adjacent))
            rows = (await conn.execute(stmt)).all()
        return [_node(row) for row in rows]

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

    async def edge_list(self) -> list[GraphEdge]:
        async with self._client.engine.connect() as conn:
            rows = (await conn.execute(self._edge_select())).all()
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
    def _edge_select() -> Select[tuple[str, str, str, bytes]]:
        return select(
            graph_edges.c.src,
            graph_edges.c.dst,
            graph_edges.c.rel_type,
            graph_edges.c.properties,
        )

    async def _count(self, stmt: Select[tuple[int]]) -> int:
        async with self._client.engine.connect() as conn:
            return int((await conn.execute(stmt)).scalar_one())
