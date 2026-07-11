"""Graph community detection (D-40): Leiden via ``leidenalg``, optional behind
``[community]`` (ADR-028, supersedes graspologic).

Slim core (D-03): ``leidenalg`` (and the ``igraph`` it rides on) import lazily
and only here. Without the extra every entry point is a clean no-op — logged at
INFO exactly once, never a warning storm and never an ImportError leaking up.

``leidenalg`` is Traag's reference Leiden implementation. Unlike graspologic it
declares no ``numpy`` dependency, so ``[community]`` no longer conflicts with the
``numpy>=2`` stack (``ingest``); see ADR-028. Its ``find_partition`` returns a
*flat* partition, so the hierarchical ``max_cluster_size`` bound graspologic gave
for free is reproduced here by :func:`_split_oversized` — recursively
re-partitioning any community larger than the bound (exactly what graspologic did
internally).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from memspine.config import constants
from memspine.observability.logging import get_logger
from memspine.services.graph.base import GraphEdge

if TYPE_CHECKING:
    pass

__all__ = ["communities_available", "detect_communities"]

_log = get_logger(__name__)

_absence_logged = False


def communities_available() -> bool:
    """Whether the ``[community]`` extra (leidenalg, D-40/ADR-028) is installed."""
    global _absence_logged
    try:
        import leidenalg  # noqa: F401
    except ImportError:
        if not _absence_logged:
            _log.info(
                "communities.unavailable",
                detail="leidenalg not installed — community detection is a "
                "no-op; enable with `pip install memspine[community]` (D-40)",
            )
            _absence_logged = True
        return False
    return True


def _split_oversized(
    graph: Any, resolution: float, random_seed: int, max_cluster_size: int
) -> list[list[str]]:
    """Hierarchical bound over a flat Leiden partition (graspologic parity).

    Partition ``graph`` once; any community larger than ``max_cluster_size`` that
    is a *strict* subset of the graph is re-partitioned on its induced subgraph
    and recursed. A community that spans the whole (sub)graph is indivisible by
    re-partition, so it is accepted as-is — this is the recursion's base case and
    guarantees termination. ``igraph`` subgraphs preserve the ``name``/``weight``
    attributes, so member identity survives every level.
    """
    import leidenalg

    partition = leidenalg.find_partition(
        graph,
        leidenalg.RBConfigurationVertexPartition,
        weights="weight",
        resolution_parameter=resolution,
        seed=random_seed,
        n_iterations=-1,  # iterate to convergence — deterministic under the seed
    )
    out: list[list[str]] = []
    for members in partition:
        names = [str(graph.vs[i]["name"]) for i in members]
        if len(names) > max_cluster_size and len(names) < graph.vcount():
            subgraph = graph.subgraph(members)
            out.extend(_split_oversized(subgraph, resolution, random_seed, max_cluster_size))
        else:
            out.append(names)
    return out


def detect_communities(
    edges: list[GraphEdge],
    *,
    min_size: int = 1,
    resolution: float = constants.LEIDEN_RESOLUTION,
    randomness: float = constants.LEIDEN_RANDOMNESS,
    random_seed: int = constants.LEIDEN_RANDOM_SEED,
    max_cluster_size: int = constants.LEIDEN_MAX_CLUSTER_SIZE,
) -> list[list[str]]:
    """Leiden clusters over the live association graph.

    Returns sorted member-id lists (communities ordered by their members) of
    at least ``min_size`` nodes; ``[]`` without the extra or without edges.
    Prune tombstones (weight ``<= 0``, ADR-015) and self-loops are excluded.

    ``resolution``/``max_cluster_size`` are the Leiden granularity knobs (higher
    resolution => more, smaller communities; ``max_cluster_size`` recursively
    caps community size, D-40 parity); ``random_seed`` is fixed by default so the
    same graph yields the same communities (rebuild determinism, D0.1).
    ``randomness`` is retained for config/API compatibility — leidenalg governs
    the refinement step's randomness internally, seeded deterministically by
    ``random_seed``, so the knob is accepted but not separately wired. All are
    surfaced as ``memories.associative.policies.community.*`` config (v0.2 A6).
    """
    if not communities_available():
        return []
    import igraph

    # Weighted, undirected, named graph straight from the edge list. Drop tombstone
    # edges (weight <= 0) and self-loops; TupleList mints one named vertex per id.
    tuples = [(e.src, e.dst, e.weight) for e in edges if e.weight > 0.0 and e.src != e.dst]
    if not tuples:
        return []
    graph = igraph.Graph.TupleList(tuples, weights=True, directed=False)
    communities = [
        sorted(members)
        for members in _split_oversized(graph, resolution, random_seed, max_cluster_size)
        if len(members) >= min_size
    ]
    communities.sort()
    return communities
