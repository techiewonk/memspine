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

from collections.abc import Iterable

from memspine.config.constants import PPR_DAMPING, PPR_ITERATIONS
from memspine.services.graph.base import GraphEdge

__all__ = ["personalized_pagerank"]


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
