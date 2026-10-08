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

Tenancy and traversal (KB-1/KB-2/KB-3): every node and edge carries the
``namespace`` it was projected in; ``neighbors``/``subgraph``/``edge_list``
take an optional ``namespace`` filter (None = every namespace) so PPR, BFS and
Leiden never scan another tenant's edges. ``max_degree`` caps the fan-out per
node during a walk: from each node only its ``max_degree`` strongest live
neighbours are followed (weight descending, then node id), the same rule in
every adapter (:func:`capped_neighbors`).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

__all__ = [
    "GraphEdge",
    "GraphNode",
    "GraphStore",
    "capped_neighbors",
    "edge_kind",
    "edge_namespace",
    "edge_weight",
    "walk_neighbors",
]


def edge_weight(properties: Mapping[str, object]) -> float:
    """The walk weight carried by an edge-properties mapping; edges without a
    numeric ``weight`` property default to 1.0. Shared so adapters can apply
    the tombstone contract (weight ``<= 0`` is gone, ADR-015) to raw rows."""
    raw = properties.get("weight")
    if isinstance(raw, bool) or not isinstance(raw, int | float):
        return 1.0
    return float(raw)


def edge_kind(properties: Mapping[str, object]) -> str | None:
    """The optional ``kind`` edge property (``state``/``event`` for fact edges,
    GP-1), stored in its own column so readers can filter on it."""
    raw = properties.get("kind")
    return raw if isinstance(raw, str) and raw else None


def edge_namespace(namespace: str | None, properties: Mapping[str, object] | None) -> str:
    """The namespace a node/edge is stored under: the explicit argument, else a
    ``namespace`` property (how the projector has always tagged nodes), else ""."""
    if namespace:
        return namespace
    raw = (properties or {}).get("namespace")
    return raw if isinstance(raw, str) else ""


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
        *,
        namespace: str | None = None,
    ) -> None:
        """Create or fully replace a node's labels + properties (idempotent).

        ``namespace`` defaults to the ``namespace`` property (:func:`edge_namespace`).
        """
        ...

    async def upsert_edge(
        self,
        src: str,
        dst: str,
        rel_type: str,
        properties: Mapping[str, object] | None = None,
        *,
        namespace: str | None = None,
    ) -> None:
        """Create or replace the ``(src, dst, rel_type)`` edge (idempotent).

        Missing endpoint nodes are implicitly created bare (in the edge's
        namespace), so link replay never depends on node-event ordering. The
        ``weight`` and ``kind`` properties are also stored as columns.
        """
        ...

    async def neighbors(
        self,
        node_id: str,
        rel_type: str | None = None,
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphNode]:
        """Nodes reachable within ``depth`` undirected hops (start excluded),
        nearest first, then by node id.

        Tombstoned edges (weight ``<= 0``, the ADR-015 prune marker) are not
        traversed — every reader treats them as gone. ``namespace`` restricts
        the walk to that namespace's edges; ``max_degree`` caps the fan-out per
        node (:func:`capped_neighbors`).
        """
        ...

    async def subgraph(
        self,
        seeds: Sequence[str],
        depth: int = 1,
        *,
        namespace: str | None = None,
        max_degree: int | None = None,
    ) -> list[GraphEdge]:
        """Every live edge between nodes reachable within ``depth`` hops of any
        seed (seeds included), under the same filters as :meth:`neighbors`."""
        ...

    async def edges_of(self, node_id: str) -> list[GraphEdge]:
        """Every edge touching the node, either direction (M13.6 link budget)."""
        ...

    async def delete_node(self, node_id: str) -> None:
        """Remove the node and cascade every touching edge (M7 forget)."""
        ...

    async def edge_list(self, namespace: str | None = None) -> list[GraphEdge]:
        """Edge export — the D-40 community-detection and PPR input. ``namespace``
        restricts it to one tenant (None = every edge, tombstones included)."""
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


def capped_neighbors(edges: Sequence[GraphEdge], node_id: str, max_degree: int | None) -> list[str]:
    """The neighbours a capped walk follows from ``node_id`` (KB-3).

    Live edges only; each neighbour ranks by its strongest edge to ``node_id``
    (weight descending, then node id), and the first ``max_degree`` are kept
    (None = all). The SQL and Cypher adapters implement this exact rule.
    """
    best: dict[str, float] = {}
    for edge in edges:
        if edge.weight <= 0.0 or node_id not in (edge.src, edge.dst):
            continue
        other = edge.dst if edge.src == node_id else edge.src
        if other == node_id:
            continue
        best[other] = max(best.get(other, edge.weight), edge.weight)
    ranked = sorted(best, key=lambda other: (-best[other], other))
    return ranked if max_degree is None else ranked[: max(max_degree, 0)]


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


#: GR-3 (graph engine plan 2026-10-08): the node property holding an entity's embedding
#: on adapters without a native vector column (sqlite_adjacency), and its source text.
ENTITY_VECTOR_PROP = "_entity_vec"
ENTITY_TEXT_PROP = "_entity_text"


def entity_terms(text: str) -> set[str]:
    """Lower-case word set for the BM25-like entity text match (GR-5 fallback)."""
    import re

    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 1}


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=False))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    return dot / (na * nb) if na > 0 and nb > 0 else 0.0


def fuse_entity_hits(
    by_vector: Sequence[str], by_text: Sequence[str], top_k: int, k: int = 60
) -> list[tuple[str, float]]:
    """GR-6: reciprocal-rank fusion of the cosine and text rankings of entity nodes."""
    scores: dict[str, float] = {}
    for ranking in (by_vector, by_text):
        for rank, node_id in enumerate(ranking, start=1):
            scores[node_id] = scores.get(node_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))[:top_k]
