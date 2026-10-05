"""Community detection (D-40, ADR-028, ADR-043 — graspologic-native): a clean,
logged no-op without ``[community]``, correct Leiden clusters with it, and the
built-in label propagation (KB-12)."""

from __future__ import annotations

import random
import sys

import pytest

from memspine.memories.associative import communities
from memspine.services.graph.base import GraphEdge


def _edges() -> list[GraphEdge]:
    return [
        GraphEdge(src="a", dst="b", rel_type="related", properties={"weight": 1.0}),
        GraphEdge(src="b", dst="c", rel_type="related", properties={"weight": 1.0}),
    ]


def _without_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``import graspologic_native`` fail, installed or not."""
    monkeypatch.setitem(sys.modules, "graspologic_native", None)


def test_noop_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_extra(monkeypatch)
    assert communities.detect_communities(_edges()) == []
    assert communities.partition_graph(_edges(), algorithm="leiden").labels == {}


def test_absence_is_logged_at_info_exactly_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _without_extra(monkeypatch)
    monkeypatch.setattr(communities, "_absence_logged", False)
    assert communities.communities_available() is False
    assert communities._absence_logged is True  # first call logged
    assert communities.communities_available() is False  # second call silent


def test_detection_groups_connected_components_when_installed() -> None:
    if not communities.communities_available():
        pytest.skip("graspologic-native not installed ([community] extra)")
    clusters = communities.detect_communities(_edges(), min_size=3)
    assert clusters == [["a", "b", "c"]]


def test_separates_disconnected_cliques_when_installed() -> None:
    """Two disconnected triangles are two connected components, so Leiden must
    return them as two distinct communities (multi-community output + ordering)."""
    if not communities.communities_available():
        pytest.skip("graspologic-native not installed ([community] extra)")
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
        pytest.skip("graspologic-native not installed ([community] extra)")
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
    edges = _graph(n_groups=8, size=7)
    expected = communities.detect_communities(edges)
    for seed in range(5):
        shuffled = list(edges)
        random.Random(seed).shuffle(shuffled)
        assert communities.detect_communities(shuffled) == expected


def _planted(groups: int, size: int, *, mu: float, seed: int, degree: int = 6) -> list[GraphEdge]:
    """Planted communities: each node draws ``degree`` edges, a share ``mu`` of
    them to a random node outside its group."""
    rng = random.Random(seed)
    members = [[f"c{g:03d}n{i:03d}" for i in range(size)] for g in range(groups)]
    every = [node for group in members for node in group]
    edges: list[GraphEdge] = []
    for group in members:
        for node in group:
            for _ in range(degree):
                other = rng.choice(every) if rng.random() < mu else rng.choice(group)
                if other != node:
                    weight = round(rng.uniform(0.3, 1.0), 3)
                    edges.append(GraphEdge(node, other, "related", {"weight": weight}))
    return edges


# -- built-in label propagation (KB-12 / #82) ---------------------------------


def test_lpa_tie_keeps_the_current_label_then_takes_the_lower() -> None:
    adj = {"x": {"a": 1.0, "b": 1.0}, "a": {"x": 1.0}, "b": {"x": 1.0}}
    labels = {"x": 7, "a": 7, "b": 3}
    communities.label_propagation(adj, labels, passes=1, nodes=["x"])
    assert labels["x"] == 7  # tied with label 3: the current label stays
    labels = {"x": 9, "a": 7, "b": 3}
    communities.label_propagation(adj, labels, passes=1, nodes=["x"])
    assert labels["x"] == 3  # tie between 7 and 3, current not among them: lower id


def test_lpa_respects_the_size_cap_and_the_pass_cap() -> None:
    adj = {"x": {"a": 5.0, "b": 1.0}, "a": {"x": 5.0}, "b": {"x": 1.0}}
    labels = {"x": 2, "a": 0, "b": 1}
    communities.label_propagation(adj, labels, passes=1, nodes=["x"], max_size=1)
    assert labels["x"] == 2  # both neighbour labels are full; x stays
    labels = {node: i for i, node in enumerate(sorted(adj))}
    assert communities.label_propagation(adj, labels, passes=0) == 0


def test_lpa_alone_separates_planted_groups_and_ignores_edge_order() -> None:
    edges = _planted(6, 12, mu=0.05, seed=3)
    result = communities.partition_graph(edges, algorithm="lpa")
    assert not result.collapsed
    assert len(result.communities(3)) >= 5
    for seed in range(3):
        shuffled = list(edges)
        random.Random(seed).shuffle(shuffled)
        assert communities.partition_graph(shuffled, algorithm="lpa").labels == result.labels


def test_collapse_guard_keeps_the_previous_partition() -> None:
    """KB-12: LPA merging most of a graph of >= 100 nodes into one community is
    rejected; the previous partition (restricted to live nodes) is kept."""
    rng = random.Random(1)
    nodes = [f"n{i:03d}" for i in range(150)]
    edges = [
        GraphEdge(a, b, "related", {"weight": 1.0})
        for i, a in enumerate(nodes)
        for b in nodes[i + 1 :]
        if rng.random() < 0.3
    ]
    previous = {node: i % 5 for i, node in enumerate(nodes)}
    result = communities.partition_graph(edges, algorithm="lpa", previous=previous)
    assert result.collapsed
    assert result.labels == communities._canonical_labels(previous)
    # No previous partition: a collapse yields no communities, never one giant one.
    bare = communities.partition_graph(edges, algorithm="lpa")
    assert bare.collapsed and bare.communities() == []


def test_collapse_guard_ignores_small_graphs() -> None:
    edges = [GraphEdge(f"a{i}", f"a{j}", "related") for i in range(6) for j in range(i + 1, 6)]
    result = communities.partition_graph(edges, algorithm="lpa")
    assert not result.collapsed and len(result.communities()) == 1


def test_incremental_places_new_nodes_by_neighbour_majority() -> None:
    edges = _planted(4, 10, mu=0.0, seed=2)
    first = communities.partition_graph(edges, algorithm="lpa")
    newcomer = [
        GraphEdge("zz-new", "c001n000", "related", {"weight": 1.0}),
        GraphEdge("zz-new", "c001n001", "related", {"weight": 1.0}),
        GraphEdge("zz-new", "c002n000", "related", {"weight": 0.5}),
    ]
    result = communities.partition_graph(
        edges + newcomer, algorithm="lpa", mode="incremental", previous=first.labels
    )
    assert result.mode == "incremental" and result.placed == 1
    assert result.labels["zz-new"] == result.labels["c001n000"]
    # Existing co-memberships survive the incremental step untouched.
    before = first.communities()
    after = [[n for n in c if n != "zz-new"] for c in result.communities()]
    assert sorted(after) == before


def test_incremental_without_a_previous_partition_is_a_full_build() -> None:
    edges = _planted(3, 8, mu=0.0, seed=4)
    result = communities.partition_graph(edges, algorithm="lpa", mode="incremental")
    assert result.mode == "full"


# -- Leiden (graspologic-native, ADR-043) ----------------------------------------


def _need_extra() -> None:
    if not communities.communities_available():
        pytest.skip("graspologic-native not installed ([community] extra)")


def _co_membership_churn(before: dict[str, int], after: dict[str, int]) -> float:
    """Share of node pairs together before and split after (nodes in both)."""
    nodes = sorted(set(before) & set(after))
    together = [
        (a, b) for i, a in enumerate(nodes) for b in nodes[i + 1 :] if before[a] == before[b]
    ]
    split = sum(1 for a, b in together if after[a] != after[b])
    return split / max(1, len(together))


def test_leiden_is_order_independent_above_ten_thousand_edges() -> None:
    """KB-13: graspologic-native's result depends on edge order above ~10K
    edges; canonical order makes the shuffled graph give the same partition."""
    _need_extra()
    edges = _planted(60, 40, mu=0.4, seed=5)
    assert len(communities.canonical_edges(edges)) > 10_000
    expected = communities.partition_graph(edges)
    for seed in range(2):
        shuffled = list(edges)
        random.Random(seed).shuffle(shuffled)
        assert communities.partition_graph(shuffled).labels == expected.labels


def test_leiden_warm_start_is_a_fixed_point_and_stable_under_small_change() -> None:
    _need_extra()
    edges = _planted(20, 15, mu=0.3, seed=6)
    first = communities.partition_graph(edges)
    assert communities.partition_graph(edges, previous=first.labels).labels == first.labels
    perturbed = edges[: len(edges) * 97 // 100] + [
        GraphEdge(f"new{i}", f"c{i:03d}n000", "related", {"weight": 1.0}) for i in range(5)
    ]
    warm = communities.partition_graph(perturbed, previous=first.labels)
    cold = communities.partition_graph(perturbed)
    warm_churn = _co_membership_churn(first.labels, warm.labels)
    assert warm_churn <= _co_membership_churn(first.labels, cold.labels)
    assert warm_churn < 0.1


def test_leiden_refinement_never_breaks_the_size_cap() -> None:
    _need_extra()
    edges = _planted(4, 30, mu=0.2, seed=7)
    for refine in (0, 10):
        result = communities.partition_graph(edges, max_cluster_size=20, refine_passes=refine)
        assert max(len(c) for c in result.communities()) <= 20


def test_partition_result_communities_are_sorted_and_filtered() -> None:
    result = communities.PartitionResult(
        labels={"b": 1, "a": 1, "z": 0, "c": 2, "d": 2, "e": 2}, mode="full"
    )
    assert result.communities() == [["a", "b"], ["c", "d", "e"], ["z"]]
    assert result.communities(3) == [["c", "d", "e"]]


def _dense_core() -> tuple[list[GraphEdge], dict[str, int]]:
    """A 70-node clique plus eight 5-node groups (110 nodes): a legitimate
    partition whose largest community already holds > 50% of the graph."""
    core = [f"core{i:02d}" for i in range(70)]
    edges = [
        GraphEdge(a, b, "related", {"weight": 1.0})
        for i, a in enumerate(core)
        for b in core[i + 1 :]
    ]
    previous = {node: 0 for node in core}
    for g in range(8):
        group = [f"g{g}n{i}" for i in range(5)]
        edges += [
            GraphEdge(a, b, "related", {"weight": 1.0})
            for i, a in enumerate(group)
            for b in group[i + 1 :]
        ]
        previous.update({node: g + 1 for node in group})
    return edges, previous


def test_incremental_guard_judges_growth_not_an_absolute_share() -> None:
    """A dense core above 50% is not a collapse when it did not grow: placing
    one new node into a small group must not trip the guard (fix/graph-review #1)."""
    edges, previous = _dense_core()
    newcomer = [GraphEdge("zz-new", "g0n0", "related", {"weight": 1.0})]
    result = communities.partition_graph(
        edges + newcomer, algorithm="lpa", mode="incremental", previous=previous
    )
    assert not result.collapsed
    assert result.placed == 1 and result.labels["zz-new"] == result.labels["g0n0"]


def test_incremental_guard_still_fires_when_the_largest_community_balloons() -> None:
    """Growth far past the previous largest community is still a collapse."""
    edges, _ = _dense_core()
    core = [f"core{i:02d}" for i in range(70)]
    # The clique was previously split into 14 groups of 5: LPA merges them.
    previous = {node: i // 5 for i, node in enumerate(core)}
    previous.update({f"g{g}n{i}": 20 + g for g in range(8) for i in range(5)})
    bridged = edges + [GraphEdge("zz-new", node, "related", {"weight": 1.0}) for node in core]
    grown = communities.partition_graph(
        bridged, algorithm="lpa", mode="incremental", previous=previous
    )
    assert grown.collapsed
    assert grown.labels == communities._canonical_labels(previous)
