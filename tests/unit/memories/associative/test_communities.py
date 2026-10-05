"""Community detection (D-40, ADR-028 — leidenalg): a clean, logged no-op
without ``[community]``, and correct Leiden clusters with it."""

from __future__ import annotations

import pytest

from memspine.memories.associative import communities
from memspine.services.graph.base import GraphEdge


def _edges() -> list[GraphEdge]:
    return [
        GraphEdge(src="a", dst="b", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="b", dst="c", rel_type="related", properties={"weight": 1.0}),
    ]


def test_noop_without_the_extra() -> None:
    if communities.communities_available():
        pytest.skip("leidenalg installed — the no-op path is not reachable")
    assert communities.detect_communities(_edges()) == []


def test_absence_is_logged_at_info_exactly_once() -> None:
    if communities.communities_available():
        pytest.skip("leidenalg installed — the no-op path is not reachable")
    communities._absence_logged = False
    assert communities.communities_available() is False
    assert communities._absence_logged is True  # first call logged
    assert communities.communities_available() is False  # second call silent


def test_detection_groups_connected_components_when_installed() -> None:
    if not communities.communities_available():
        pytest.skip("leidenalg not installed ([community] extra)")
    clusters = communities.detect_communities(_edges(), min_size=3)
    assert clusters == [["a", "b", "c"]]


def test_separates_disconnected_cliques_when_installed() -> None:
    """Two disconnected triangles are two connected components, so Leiden must
    return them as two distinct communities (multi-community output + ordering)."""
    if not communities.communities_available():
        pytest.skip("leidenalg not installed ([community] extra)")
    edges = [
        GraphEdge(src="a1", dst="a2", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="a1", dst="a3", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="a2", dst="a3", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="b1", dst="b2", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="b1", dst="b3", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="b2", dst="b3", rel_type="related", properties={"weight": 1.0}),
    ]
    assert communities.detect_communities(edges, min_size=3) == [
        ["a1", "a2", "a3"],
        ["b1", "b2", "b3"],
    ]


def test_leiden_knobs_are_accepted_and_deterministic() -> None:
    """v0.2 A6: the surfaced resolution/randomness/seed/max_cluster_size knobs
    are accepted and a fixed seed keeps the result reproducible."""
    if not communities.communities_available():
        pytest.skip("leidenalg not installed ([community] extra)")
    kwargs = dict(min_size=3, resolution=1.0, randomness=0.001, random_seed=1, max_cluster_size=10)
    first = communities.detect_communities(_edges(), **kwargs)  # type: ignore[arg-type]
    second = communities.detect_communities(_edges(), **kwargs)  # type: ignore[arg-type]
    assert first == second == [["a", "b", "c"]]


def _graph(n_groups: int = 4, size: int = 6) -> list[GraphEdge]:
    """Dense groups chained by one weak bridge each (several distinct communities)."""
    edges: list[GraphEdge] = []
    for g in range(n_groups):
        ids = [f"g{g}n{i}" for i in range(size)]
        edges += [
            GraphEdge(src=a, dst=b, rel_type="related", properties={"weight": 1.0})
            for i, a in enumerate(ids)
            for b in ids[i + 1 :]
        ]
        if g:
            edges.append(
                GraphEdge(
                    src=f"g{g - 1}n0", dst=f"g{g}n0", rel_type="related", properties={"weight": 0.2}
                )
            )
    return edges


def test_canonical_edges_ignore_store_order_and_direction() -> None:
    """KB-13 (#86): the same graph in any edge order and either direction gives
    one canonical edge list, so the detector never sees store order."""
    import random

    edges = _graph()
    shuffled = list(edges)
    random.Random(7).shuffle(shuffled)
    flipped = [
        GraphEdge(src=e.dst, dst=e.src, rel_type=e.rel_type, properties=e.properties)
        for e in shuffled
    ]
    canonical = communities.canonical_edges(edges)
    assert (
        canonical == communities.canonical_edges(shuffled) == communities.canonical_edges(flipped)
    )
    assert canonical == sorted(canonical)
    assert all(src < dst for src, dst, _w in canonical)


def test_canonical_edges_drop_tombstones_self_loops_and_sum_parallels() -> None:
    edges = [
        GraphEdge(src="b", dst="a", rel_type="related", properties={"weight": 0.5}),
        GraphEdge(src="a", dst="b", rel_type="asserted", properties={"weight": 0.25}),
        GraphEdge(src="a", dst="a", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="a", dst="c", rel_type="related", properties={"weight": 0.0}),
    ]
    assert communities.canonical_edges(edges) == [("a", "b", 0.75)]


def test_shuffled_edges_give_an_identical_partition_when_installed() -> None:
    if not communities.communities_available():
        pytest.skip("[community] extra not installed")
    import random

    edges = _graph(n_groups=8, size=7)
    expected = communities.detect_communities(edges)
    for seed in range(5):
        shuffled = list(edges)
        random.Random(seed).shuffle(shuffled)
        assert communities.detect_communities(shuffled) == expected
