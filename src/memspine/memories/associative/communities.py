"""Graph community detection (D-40): graspologic hierarchical Leiden,
optional behind ``[community]``.

Slim core (D-03): graspologic (and the networkx it brings) imports lazily and
only here. Without the extra every entry point is a clean no-op — logged at
INFO exactly once, never a warning storm and never an ImportError leaking up.
"""

from __future__ import annotations

from memspine.observability.logging import get_logger
from memspine.services.graph.base import GraphEdge

__all__ = ["communities_available", "detect_communities"]

_log = get_logger(__name__)

#: Fixed Leiden seed: same graph => same communities (rebuild determinism).
_LEIDEN_SEED = 1

_absence_logged = False


def communities_available() -> bool:
    """Whether the ``[community]`` extra (graspologic, D-40) is installed."""
    global _absence_logged
    try:
        import graspologic.partition  # noqa: F401
    except ImportError:
        if not _absence_logged:
            _log.info(
                "communities.unavailable",
                detail="graspologic not installed — community detection is a "
                "no-op; enable with `pip install memspine[community]` (D-40)",
            )
            _absence_logged = True
        return False
    return True


def detect_communities(edges: list[GraphEdge], *, min_size: int = 1) -> list[list[str]]:
    """Hierarchical-Leiden clusters over the live association graph.

    Returns sorted member-id lists (communities ordered by their members) of
    at least ``min_size`` nodes; ``[]`` without the extra or without edges.
    Prune tombstones (weight ``<= 0``, ADR-015) are excluded from the graph.
    """
    if not communities_available():
        return []
    import networkx as nx
    from graspologic.partition import hierarchical_leiden

    graph = nx.Graph()
    for edge in edges:
        if edge.weight > 0.0 and edge.src != edge.dst:
            graph.add_edge(edge.src, edge.dst, weight=edge.weight)
    if graph.number_of_edges() == 0:
        return []
    clusters = hierarchical_leiden(graph, random_seed=_LEIDEN_SEED)
    membership: dict[int, list[str]] = {}
    for entry in clusters:
        if entry.is_final_cluster:
            membership.setdefault(int(entry.cluster), []).append(str(entry.node))
    communities = [sorted(members) for members in membership.values() if len(members) >= min_size]
    communities.sort()
    return communities
