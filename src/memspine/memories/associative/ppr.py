"""Personalized PageRank over the association graph (plan §5 Phase 6, D-40).

Pure Python by design (slim core, D-03): the associative graphs the budget
(M13.6) allows are small — a bounded power iteration over ``edge_list()``
needs no numpy. Edges are walked *undirected* (a link expresses relatedness,
not order — same convention as ``GraphStore.neighbors``) and weighted; prune
tombstones (weight ``<= 0``, ADR-015) never contribute.

Deterministic: iteration order is sorted everywhere and ties in the final
ranking break on node id, so the same graph always ranks the same.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable

from memspine.config.constants import (
    GRAPH_RERANK_PPR_ALPHA,
    GRAPH_RERANK_PPR_EPSILON,
    GRAPH_RERANK_PUSH_MAX,
    PPR_DAMPING,
    PPR_ITERATIONS,
)
from memspine.services.graph.base import GraphEdge

__all__ = ["local_push_ppr", "personalized_pagerank"]


def personalized_pagerank(
    edges: Iterable[GraphEdge],
    seeds: set[str],
    *,
    damping: float = PPR_DAMPING,
    iterations: int = PPR_ITERATIONS,
    top_k: int | None = None,
) -> list[tuple[str, float]]:
    """Rank nodes by relatedness to ``seeds``; seeds themselves are excluded.

    Returns ``(node_id, score)`` sorted by score desc (node id asc on ties),
    truncated to ``top_k`` when given. Empty when no seed touches the graph.
    """
    iterations = min(max(iterations, 1), PPR_ITERATIONS)  # hard cap (D-40)
    adjacency: dict[str, dict[str, float]] = {}
    for edge in edges:
        weight = edge.weight
        if weight <= 0.0 or edge.src == edge.dst:
            continue
        adjacency.setdefault(edge.src, {})
        adjacency.setdefault(edge.dst, {})
        # Undirected: accumulate parallel edges (e.g. related + derived_from).
        adjacency[edge.src][edge.dst] = adjacency[edge.src].get(edge.dst, 0.0) + weight
        adjacency[edge.dst][edge.src] = adjacency[edge.dst].get(edge.src, 0.0) + weight
    live_seeds = sorted(seeds & set(adjacency))
    if not live_seeds:
        return []
    nodes = sorted(adjacency)
    restart = {node: (1.0 / len(live_seeds) if node in seeds else 0.0) for node in nodes}
    weighted_degree = {node: sum(adjacency[node].values()) for node in nodes}
    rank = dict(restart)
    for _ in range(iterations):
        fresh = {node: (1.0 - damping) * restart[node] for node in nodes}
        for node in nodes:
            degree = weighted_degree[node]
            if degree <= 0.0:
                continue
            share = damping * rank[node] / degree
            for neighbour, weight in adjacency[node].items():
                fresh[neighbour] += share * weight
        rank = fresh
    ranked = sorted(
        ((node, score) for node, score in rank.items() if node not in seeds and score > 0.0),
        key=lambda pair: (-pair[1], pair[0]),
    )
    return ranked if top_k is None else ranked[:top_k]


def _undirected(edges: Iterable[GraphEdge]) -> dict[str, dict[str, float]]:
    adjacency: dict[str, dict[str, float]] = {}
    for edge in edges:
        weight = edge.weight
        if weight <= 0.0 or edge.src == edge.dst:
            continue
        adjacency.setdefault(edge.src, {})
        adjacency.setdefault(edge.dst, {})
        adjacency[edge.src][edge.dst] = adjacency[edge.src].get(edge.dst, 0.0) + weight
        adjacency[edge.dst][edge.src] = adjacency[edge.dst].get(edge.src, 0.0) + weight
    return adjacency


def local_push_ppr(
    edges: Iterable[GraphEdge],
    seeds: set[str],
    *,
    alpha: float = GRAPH_RERANK_PPR_ALPHA,
    epsilon: float = GRAPH_RERANK_PPR_EPSILON,
    max_pushes: int = GRAPH_RERANK_PUSH_MAX,
) -> dict[str, float]:
    """#22: approximate personalized PageRank by local push (Andersen, Chung & Lang).

    Only the nodes the push reaches are touched, so the cost follows the
    neighbourhood of ``seeds`` (the read path passes a ``subgraph()``), never the
    whole graph. Edges are undirected and weighted like :func:`personalized_pagerank`;
    tombstones and self-loops never contribute. A node is pushed while its residual
    exceeds ``epsilon`` x its weighted degree; the lazy-walk push keeps half of the
    non-restart mass on the node. Deterministic: the queue is FIFO and every node's
    neighbours are visited in id order. Returns ``{node: score}`` for nodes with a
    positive estimate, seeds included (the caller drops what it does not rank).
    """
    adjacency = _undirected(edges)
    live = sorted(seeds & set(adjacency))
    if not live:
        return {}
    degree = {node: sum(neighbours.values()) for node, neighbours in adjacency.items()}
    estimate: dict[str, float] = {}
    residual = {node: 1.0 / len(live) for node in live}
    queue = deque(live)
    queued = set(live)
    pushes = 0
    while queue and pushes < max_pushes:
        node = queue.popleft()
        queued.discard(node)
        mass = residual.get(node, 0.0)
        if mass <= epsilon * degree[node]:
            continue
        pushes += 1
        estimate[node] = estimate.get(node, 0.0) + alpha * mass
        spread = (1.0 - alpha) * mass / 2.0
        residual[node] = spread
        for neighbour in sorted(adjacency[node]):
            residual[neighbour] = (
                residual.get(neighbour, 0.0) + spread * adjacency[node][neighbour] / degree[node]
            )
            if neighbour not in queued and residual[neighbour] > epsilon * degree[neighbour]:
                queue.append(neighbour)
                queued.add(neighbour)
        if node not in queued and residual[node] > epsilon * degree[node]:
            queue.append(node)
            queued.add(node)
    return {node: score for node, score in estimate.items() if score > 0.0}
