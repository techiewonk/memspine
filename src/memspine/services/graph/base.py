"""GraphStore port (D-26): the typed adjacency surface associative memory needs.

Graph stores are projections (D0.1): rebuildable from the event log by
replaying link events — ``clear()`` exists so a rebuild can start from zero.
The surface is deliberately minimal (typed methods, no Cypher): M13.6 link
budgets need ``edges_of``/``node_count``/``edge_count``, PPR walks need
``neighbors`` plus edge weights (the ``weight`` edge property), and D-40
community detection consumes ``edge_list()``.

Edges are stored directed (``src -> dst``) but ``neighbors`` traverses them
undirected: an associative link expresses relatedness, not order, so recall
must reach a memory from either endpoint.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = ["GraphEdge", "GraphNode", "GraphStore", "edge_weight", "walk_neighbors"]


def edge_weight(properties: Mapping[str, object]) -> float:
    """The walk weight carried by an edge-properties mapping; edges without a
    numeric ``weight`` property default to 1.0. Shared so adapters can apply
    the tombstone contract (weight ``<= 0`` is gone, ADR-015) to raw rows."""
    raw = properties.get("weight")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 1.0
    return float(raw)


@dataclass(frozen=True)
class GraphNode:
    node_id: str
    labels: tuple[str, ...] = ()
    properties: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class GraphEdge:
    src: str
    dst: str
    rel_type: str
    properties: dict[str, object] = field(default_factory=dict)

    @property
    def weight(self) -> float:
        """PPR walk weight; edges without a numeric ``weight`` property are 1.0."""
        return edge_weight(self.properties)


@runtime_checkable
class GraphStore(Protocol):
    async def upsert_node(
        self,
        node_id: str,
        labels: Sequence[str] = (),
        properties: Mapping[str, object] | None = None,
    ) -> None:
        """Create or fully replace a node's labels + properties (idempotent)."""
        ...

    async def upsert_edge(
        self,
        src: str,
        dst: str,
        rel_type: str,
        properties: Mapping[str, object] | None = None,
    ) -> None:
        """Create or replace the ``(src, dst, rel_type)`` edge (idempotent).

        Missing endpoint nodes are implicitly created bare, so link replay
        never depends on node-event ordering.
        """
        ...

    async def neighbors(
        self, node_id: str, rel_type: str | None = None, depth: int = 1
    ) -> list[GraphNode]:
        """Nodes reachable within ``depth`` undirected hops (start excluded).

        Tombstoned edges (weight ``<= 0``, the ADR-015 prune marker) are not
        traversed — every reader treats them as gone.
        """
        ...

    async def edges_of(self, node_id: str) -> list[GraphEdge]:
        """Every edge touching the node, either direction (M13.6 link budget)."""
        ...

    async def delete_node(self, node_id: str) -> None:
        """Remove the node and cascade every touching edge (M7 forget)."""
        ...

    async def edge_list(self) -> list[GraphEdge]:
        """Full edge export — the D-40 community-detection input."""
        ...

    async def node_count(self) -> int: ...

    async def edge_count(self) -> int: ...

    async def clear(self) -> None:
        """Drop every node and edge — projector rebuild support (D0.1)."""
        ...

    async def close(self) -> None:
        """Release store-held handles. Connections belong to clients (D-22/D-24),
        so for most adapters this is a no-op; it exists so callers can treat
        every adapter uniformly at shutdown."""
        ...


#: One undirected hop: ``(node_id, rel_type filter) -> adjacent nodes``.
AdjacencyFn = Callable[[str, str | None], Awaitable[list[GraphNode]]]


async def walk_neighbors(
    one_hop: AdjacencyFn, node_id: str, rel_type: str | None, depth: int
) -> list[GraphNode]:
    """Shared BFS over an adapter's single-hop primitive.

    Every adapter gets identical depth/visited semantics from one
    implementation instead of re-deriving (and diverging on) them.
    """
    seen = {node_id}
    frontier = [node_id]
    found: list[GraphNode] = []
    for _ in range(max(depth, 0)):
        next_frontier: list[str] = []
        for current in frontier:
            for node in await one_hop(current, rel_type):
                if node.node_id not in seen:
                    seen.add(node.node_id)
                    found.append(node)
                    next_frontier.append(node.node_id)
        if not next_frontier:
            break
        frontier = next_frontier
    return found
