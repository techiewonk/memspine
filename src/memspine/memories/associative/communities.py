"""Graph community detection (D-40, ADR-028, ADR-043): a hybrid Leiden → LPA
partitioner, Leiden optional behind ``[community]``.

Slim core (D-03): ``graspologic_native`` (MIT, a Rust Leiden; ADR-043 replaces
the GPL ``leidenalg`` + ``igraph``) imports lazily and only here. Without the
extra the Leiden entry points are clean no-ops, logged at INFO exactly once.
The built-in label propagation (LPA) is pure Python and always available.

The hybrid (KB-12, evidence in ``paper_spine/evaluation/community/
COMMUNITY_HYBRID_2026-10-05.md``):

* **Full build / refresh:** Leiden over the canonical edge list (KB-13), seeded,
  warm-started from the previous partition when there is one, with
  ``hierarchical_leiden`` enforcing ``max_cluster_size``; then up to
  ``refine_passes`` LPA passes that only move boundary nodes.
* **Incremental (per sleep):** new nodes take their neighbours' majority label,
  then at most ``incremental_passes`` LPA passes over the touched nodes.
* **LPA alone** (``algorithm: lpa``, no extra needed) runs to convergence from
  the previous partition (or singletons), guarded against collapse.

LPA is asynchronous over sorted nodes and weighted; a node keeps its current
label on a tie, otherwise takes the lowest tied label, so every result is a
pure function of the canonical edge list and the starting partition.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Literal

from memspine.config import constants
from memspine.observability.logging import get_logger
from memspine.services.graph.base import GraphEdge

__all__ = [
    "PartitionResult",
    "canonical_edges",
    "communities_available",
    "detect_communities",
    "label_propagation",
    "partition_graph",
]

_log = get_logger(__name__)

_absence_logged = False

Adjacency = dict[str, dict[str, float]]
Mode = Literal["full", "incremental"]


def communities_available() -> bool:
    """Whether the ``[community]`` extra (graspologic-native, ADR-043) is installed."""
    global _absence_logged
    try:
        import graspologic_native  # noqa: F401
    except ImportError:
        if not _absence_logged:
            _log.info(
                "communities.unavailable",
                detail="graspologic-native not installed — Leiden community detection "
                "is a no-op; enable with `pip install memspine[community]` (D-40)",
            )
            _absence_logged = True
        return False
    return True


def canonical_edges(edges: Iterable[GraphEdge]) -> list[tuple[str, str, float]]:
    """The undirected, store-order-independent form of ``edges`` (KB-13).

    Tombstones (weight ``<= 0``, ADR-015) and self-loops are dropped; each pair
    is keyed ``(min, max)`` and parallel edges (either direction) are summed in
    sorted weight order, so the float sum is order-independent too. The result
    is sorted by ``(src, dst)``: the same graph in any edge order yields the
    same list, hence the same partition (rebuild determinism, D0.1).
    """
    weights: dict[tuple[str, str], list[float]] = {}
    for edge in edges:
        weight = edge.weight
        if weight <= 0.0 or edge.src == edge.dst:
            continue
        pair = (edge.src, edge.dst) if edge.src < edge.dst else (edge.dst, edge.src)
        weights.setdefault(pair, []).append(weight)
    return [(src, dst, sum(sorted(ws))) for (src, dst), ws in sorted(weights.items())]


def _adjacency(tuples: list[tuple[str, str, float]]) -> Adjacency:
    adj: Adjacency = {}
    for src, dst, weight in tuples:
        adj.setdefault(src, {})[dst] = weight
        adj.setdefault(dst, {})[src] = weight
    return adj


def _canonical_labels(labels: Mapping[str, int]) -> dict[str, int]:
    """Relabel ``0..k-1`` in order of each community's smallest member, so the
    numbering (and with it every lower-label tie-break) depends only on the
    partition, never on how an earlier run happened to number it."""
    first: dict[int, str] = {}
    for node in sorted(labels):
        first.setdefault(labels[node], node)
    order = {label: i for i, label in enumerate(sorted(first, key=first.__getitem__))}
    return {node: order[label] for node, label in sorted(labels.items())}


def _sizes(labels: Mapping[str, int]) -> dict[int, int]:
    sizes: dict[int, int] = {}
    for label in labels.values():
        sizes[label] = sizes.get(label, 0) + 1
    return sizes


def label_propagation(
    adj: Adjacency,
    labels: dict[str, int],
    *,
    passes: int,
    nodes: Iterable[str] | None = None,
    max_size: int | None = None,
) -> int:
    """Weighted asynchronous LPA, in place over ``labels``; returns nodes moved.

    Visits ``nodes`` (default: all of ``adj``) in sorted order, at most
    ``passes`` times, stopping early once a pass moves nothing. A node moves to
    the label carrying the most neighbour weight; on a tie it keeps its current
    label, otherwise takes the lowest tied label. A label already holding
    ``max_size`` members accepts no newcomer, so refinement can never undo the
    ``max_cluster_size`` bound.
    """
    order = sorted(adj if nodes is None else set(nodes) & adj.keys())
    sizes = _sizes(labels)
    moved = 0
    for _ in range(passes):
        changed = 0
        for node in order:
            current = labels[node]
            scores: dict[int, float] = {}
            for neighbour, weight in adj[node].items():
                label = labels[neighbour]
                scores[label] = scores.get(label, 0.0) + weight
            if max_size is not None:
                scores = {
                    label: score
                    for label, score in scores.items()
                    if label == current or sizes.get(label, 0) < max_size
                }
            if not scores:
                continue
            best = max(scores.values())
            if scores.get(current) == best:
                continue
            target = min(label for label, score in scores.items() if score == best)
            sizes[current] -= 1
            sizes[target] = sizes.get(target, 0) + 1
            labels[node] = target
            changed += 1
        moved += changed
        if not changed:
            break
    return moved


def _warm_start(adj: Adjacency, previous: Mapping[str, int]) -> tuple[dict[str, int], list[str]]:
    """The previous partition restricted to the live graph, with every new node
    placed by one neighbour-majority vote (sorted order, so a new node sees the
    new nodes placed before it). Returns ``(labels, new_nodes)``."""
    labels = {node: previous[node] for node in sorted(adj) if node in previous}
    fresh = max(labels.values(), default=-1) + 1
    new_nodes = [node for node in sorted(adj) if node not in labels]
    for node in new_nodes:
        scores: dict[int, float] = {}
        for neighbour, weight in adj[node].items():
            if neighbour in labels:
                scores[labels[neighbour]] = scores.get(labels[neighbour], 0.0) + weight
        if scores:
            best = max(scores.values())
            labels[node] = min(label for label, score in scores.items() if score == best)
        else:
            labels[node] = fresh
            fresh += 1
    return labels, new_nodes


def _collapsed(labels: Mapping[str, int], baseline: int = 0) -> bool:
    """LPA's failure mode (KB-12): one label swallowing most of a real graph.

    ``baseline`` is the previous partition's largest community (over the live
    nodes). A legitimately dense core can already hold more than the share
    (Leiden is not guarded), so with a baseline the guard judges *growth*: the
    largest community must also exceed the baseline by more than
    ``COMMUNITY_COLLAPSE_GROWTH`` of itself — otherwise the guard would fire
    on every run after such a partition and lock it (ADR-015 amendment).
    """
    if len(labels) < constants.COMMUNITY_COLLAPSE_MIN_NODES:
        return False
    largest = max(_sizes(labels).values())
    if largest <= constants.COMMUNITY_COLLAPSE_SHARE * len(labels):
        return False
    return largest > baseline * (1.0 + constants.COMMUNITY_COLLAPSE_GROWTH)


def _leiden(
    tuples: list[tuple[str, str, float]],
    start: Mapping[str, int] | None,
    *,
    resolution: float,
    randomness: float,
    random_seed: int,
    max_cluster_size: int,
) -> dict[str, int]:
    """graspologic-native ``hierarchical_leiden``: the final level of each node.

    Oversized clusters are re-partitioned natively (this replaces ADR-028's
    ``_split_oversized`` recursion); a dense cluster that cannot be split is
    accepted as it is.
    """
    import graspologic_native as gn

    clusters = gn.hierarchical_leiden(
        tuples,
        starting_communities=dict(start) if start else None,
        resolution=resolution,
        randomness=randomness,
        iterations=constants.LEIDEN_ITERATIONS,
        use_modularity=True,
        max_cluster_size=max_cluster_size,
        seed=random_seed,
    )
    return {str(c.node): int(c.cluster) for c in clusters if c.is_final_cluster}


@dataclass(frozen=True)
class PartitionResult:
    """One partition run. ``labels`` maps every live node to its community.

    ``collapsed`` means the collapse guard fired: ``labels`` is then the
    previous partition restricted to the live graph and callers should treat the
    run as "no change". ``placed`` counts nodes placed incrementally (new since
    the previous partition) in an ``incremental`` run.
    """

    labels: dict[str, int]
    mode: Mode
    placed: int = 0
    moved: int = 0
    collapsed: bool = False
    new_nodes: list[str] = field(default_factory=list)

    def communities(self, min_size: int = 1) -> list[list[str]]:
        """Sorted member lists of at least ``min_size`` nodes, sorted."""
        groups: dict[int, list[str]] = {}
        for node, label in sorted(self.labels.items()):
            groups.setdefault(label, []).append(node)
        return sorted(members for members in groups.values() if len(members) >= min_size)


def partition_graph(
    edges: Iterable[GraphEdge],
    *,
    algorithm: Literal["leiden", "lpa"] = "leiden",
    mode: Mode = "full",
    previous: Mapping[str, int] | None = None,
    resolution: float = constants.LEIDEN_RESOLUTION,
    randomness: float = constants.LEIDEN_RANDOMNESS,
    random_seed: int = constants.LEIDEN_RANDOM_SEED,
    max_cluster_size: int = constants.LEIDEN_MAX_CLUSTER_SIZE,
    refine_passes: int = constants.COMMUNITY_REFINE_PASSES,
    incremental_passes: int = constants.COMMUNITY_INCREMENTAL_PASSES,
) -> PartitionResult:
    """Partition the live association graph (KB-12 hybrid, see module docstring).

    ``mode="incremental"`` needs a ``previous`` partition (without one it runs
    a full build). ``algorithm="leiden"`` needs the extra; without it an empty
    result is returned (the D-40 no-op). The collapse guard applies to every
    LPA-only result (``algorithm="lpa"`` and incremental runs).
    """
    tuples = canonical_edges(edges)
    if not tuples:
        return PartitionResult(labels={}, mode=mode)
    adj = _adjacency(tuples)
    previous = _canonical_labels(previous) if previous else None
    if mode == "incremental" and previous:
        labels, new_nodes = _warm_start(adj, previous)
        touched = set(new_nodes)
        for node in new_nodes:
            touched.update(adj[node])
        moved = label_propagation(
            adj, labels, passes=incremental_passes, nodes=touched, max_size=max_cluster_size
        )
        return _guarded(labels, previous, adj, "incremental", len(new_nodes), moved, new_nodes)
    start, new_nodes = _warm_start(adj, previous) if previous else (None, sorted(adj))
    if algorithm == "lpa":
        labels = dict(start) if start else {node: i for i, node in enumerate(sorted(adj))}
        moved = label_propagation(
            adj, labels, passes=constants.COMMUNITY_LPA_MAX_PASSES, max_size=max_cluster_size
        )
        return _guarded(labels, previous or {}, adj, "full", 0, moved, new_nodes)
    if not communities_available():
        return PartitionResult(labels={}, mode="full")
    labels = _canonical_labels(
        _leiden(
            tuples,
            _canonical_labels(start) if start else None,
            resolution=resolution,
            randomness=randomness,
            random_seed=random_seed,
            max_cluster_size=max_cluster_size,
        )
    )
    moved = label_propagation(adj, labels, passes=refine_passes, max_size=max_cluster_size)
    return PartitionResult(
        labels=_canonical_labels(labels), mode="full", moved=moved, new_nodes=new_nodes
    )


def _guarded(
    labels: dict[str, int],
    previous: Mapping[str, int],
    adj: Adjacency,
    mode: Mode,
    placed: int,
    moved: int,
    new_nodes: list[str],
) -> PartitionResult:
    live_previous = {node: previous[node] for node in adj if node in previous}
    baseline = max(_sizes(live_previous).values(), default=0)
    if _collapsed(labels, baseline):
        _log.warning(
            "communities.collapse_guard",
            nodes=len(labels),
            largest=max(_sizes(labels).values()),
            detail="label propagation merged most of the graph into one community; "
            "the previous partition is kept",
        )
        kept = {node: previous[node] for node in sorted(adj) if node in previous}
        return PartitionResult(labels=kept, mode=mode, collapsed=True)
    return PartitionResult(
        labels=_canonical_labels(labels),
        mode=mode,
        placed=placed,
        moved=moved,
        new_nodes=new_nodes,
    )


def detect_communities(
    edges: list[GraphEdge],
    *,
    min_size: int = 1,
    resolution: float = constants.LEIDEN_RESOLUTION,
    randomness: float = constants.LEIDEN_RANDOMNESS,
    random_seed: int = constants.LEIDEN_RANDOM_SEED,
    max_cluster_size: int = constants.LEIDEN_MAX_CLUSTER_SIZE,
) -> list[list[str]]:
    """Leiden (→ LPA refinement) clusters over the live association graph.

    Returns sorted member-id lists (communities ordered by their members) of
    at least ``min_size`` nodes; ``[]`` without the extra or without edges.
    Prune tombstones (weight ``<= 0``, ADR-015) and self-loops are excluded,
    and edges are put in canonical order first (KB-13), so the result does not
    depend on the store's edge order.

    ``resolution``/``max_cluster_size`` are the Leiden granularity knobs (higher
    resolution => more, smaller communities; ``max_cluster_size`` caps community
    size); ``randomness`` is Leiden's refinement exploration; ``random_seed`` is
    fixed by default so the same graph yields the same communities (rebuild
    determinism, D0.1). All are ``memories.associative.policies.community.*``.
    """
    if not communities_available():
        return []
    result = partition_graph(
        edges,
        algorithm="leiden",
        resolution=resolution,
        randomness=randomness,
        random_seed=random_seed,
        max_cluster_size=max_cluster_size,
    )
    return result.communities(min_size)
