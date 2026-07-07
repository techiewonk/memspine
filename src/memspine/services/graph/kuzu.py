"""Kuzu graph store — the first-class embedded-Cypher alternative (D-26), ``[kuzu]``.

Consumes an injected :class:`KuzuClient` (D-22/D-24). One node table
(``MemoryNode``) and one rel table (``MemoryLink``) are created lazily on
first use; labels/properties ride as canonical orjson strings (D-38) so the
port's free-form payloads survive kuzu's fixed columnar schema.

Kuzu's Python API is synchronous — every call is pushed to a worker thread,
and a single lock serializes them (one embedded connection is not a pool).
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from typing import Any

import orjson

from memspine.clients.kuzu import KuzuClient
from memspine.services.graph.base import GraphEdge, GraphNode, edge_weight, walk_neighbors

__all__ = ["KuzuGraphStore"]

_DDL = (
    "CREATE NODE TABLE IF NOT EXISTS MemoryNode("
    "node_id STRING, labels STRING, properties STRING, PRIMARY KEY (node_id))",
    "CREATE REL TABLE IF NOT EXISTS MemoryLink("
    "FROM MemoryNode TO MemoryNode, rel_type STRING, properties STRING)",
)

_UPSERT_NODE = (
    "MERGE (n:MemoryNode {node_id: $node_id}) "
    "ON CREATE SET n.labels = $labels, n.properties = $properties "
    "ON MATCH SET n.labels = $labels, n.properties = $properties"
)

_ENSURE_NODE = (
    "MERGE (n:MemoryNode {node_id: $node_id}) ON CREATE SET n.labels = '[]', n.properties = '{}'"
)

_UPSERT_EDGE = (
    "MATCH (a:MemoryNode {node_id: $src}), (b:MemoryNode {node_id: $dst}) "
    "MERGE (a)-[e:MemoryLink {rel_type: $rel_type}]->(b) "
    "ON CREATE SET e.properties = $properties "
    "ON MATCH SET e.properties = $properties"
)

_EDGE_RETURN = "RETURN s.node_id, d.node_id, e.rel_type, e.properties"


def _loads(raw: object) -> Any:
    return orjson.loads(raw if isinstance(raw, str | bytes) else "null")


def _edge(row: list[Any]) -> GraphEdge:
    return GraphEdge(
        src=str(row[0]),
        dst=str(row[1]),
        rel_type=str(row[2]),
        properties=dict(_loads(row[3]) or {}),
    )


class KuzuGraphStore:
    def __init__(self, client: KuzuClient) -> None:
        self._client = client
        self._ready = False
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
            for statement in _DDL:
                await asyncio.to_thread(self._client.connection.execute, statement)
            self._ready = True

        def _run() -> list[list[Any]]:
            result = self._client.connection.execute(query, parameters=parameters or {})
            rows: list[list[Any]] = []
            while result.has_next():
                rows.append(list(result.get_next()))
            return rows

        return await asyncio.to_thread(_run)

    async def upsert_node(
        self,
        node_id: str,
        labels: Sequence[str] = (),
        properties: Mapping[str, object] | None = None,
    ) -> None:
        await self._execute(
            _UPSERT_NODE,
            {
                "node_id": node_id,
                "labels": orjson.dumps(list(labels)).decode(),
                "properties": orjson.dumps(dict(properties or {})).decode(),
            },
        )

    async def upsert_edge(
        self,
        src: str,
        dst: str,
        rel_type: str,
        properties: Mapping[str, object] | None = None,
    ) -> None:
        # Endpoints are implicitly created bare (port contract): link replay
        # must never depend on node-event ordering. One lock acquisition spans
        # all three statements so a concurrent caller cannot interleave
        # between the ensure-node and upsert-edge steps.
        async with self._lock:
            for endpoint in (src, dst):
                await self._execute_unlocked(_ENSURE_NODE, {"node_id": endpoint})
            await self._execute_unlocked(
                _UPSERT_EDGE,
                {
                    "src": src,
                    "dst": dst,
                    "rel_type": rel_type,
                    "properties": orjson.dumps(dict(properties or {})).decode(),
                },
            )

    async def neighbors(
        self, node_id: str, rel_type: str | None = None, depth: int = 1
    ) -> list[GraphNode]:
        return await walk_neighbors(self._one_hop, node_id, rel_type, depth)

    async def _one_hop(self, node_id: str, rel_type: str | None) -> list[GraphNode]:
        rel_pattern = "[e:MemoryLink {rel_type: $rel_type}]" if rel_type else "[e:MemoryLink]"
        rows = await self._execute(
            f"MATCH (a:MemoryNode {{node_id: $node_id}})-{rel_pattern}-(b:MemoryNode) "
            "RETURN b.node_id, b.labels, b.properties, e.properties",
            {"node_id": node_id, **({"rel_type": rel_type} if rel_type else {})},
        )
        # Tombstoned edges (weight <= 0, ADR-015) are gone for every reader —
        # a pruned neighbour must not resurface via a walk. Deduplicate by
        # node id: any one live edge keeps the neighbour reachable.
        nodes: dict[str, GraphNode] = {}
        for row in rows:
            if edge_weight(dict(_loads(row[3]) or {})) <= 0.0:
                continue
            nodes.setdefault(
                str(row[0]),
                GraphNode(
                    node_id=str(row[0]),
                    labels=tuple(_loads(row[1]) or ()),
                    properties=dict(_loads(row[2]) or {}),
                ),
            )
        return list(nodes.values())

    async def edges_of(self, node_id: str) -> list[GraphEdge]:
        rows = await self._execute(
            "MATCH (s:MemoryNode)-[e:MemoryLink]->(d:MemoryNode) "
            f"WHERE s.node_id = $node_id OR d.node_id = $node_id {_EDGE_RETURN}",
            {"node_id": node_id},
        )
        return [_edge(row) for row in rows]

    async def delete_node(self, node_id: str) -> None:
        await self._execute(
            "MATCH (n:MemoryNode {node_id: $node_id}) DETACH DELETE n", {"node_id": node_id}
        )

    async def edge_list(self) -> list[GraphEdge]:
        rows = await self._execute(
            f"MATCH (s:MemoryNode)-[e:MemoryLink]->(d:MemoryNode) {_EDGE_RETURN}"
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

    async def close(self) -> None:
        """No-op: the injected KuzuClient owns the database handle (D-24)."""
