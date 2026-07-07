"""Personalized PageRank (D-40): ranking sanity, determinism, tombstones."""

from __future__ import annotations

from memspine.memories.associative.ppr import personalized_pagerank
from memspine.services.graph.base import GraphEdge


def _edge(src: str, dst: str, weight: float = 1.0) -> GraphEdge:
    return GraphEdge(src=src, dst=dst, rel_type="related", properties={"weight": weight})


def test_star_graph_ranks_the_hub_first() -> None:
    edges = [_edge("hub", spoke) for spoke in ("a", "b", "c", "d")]
    ranked = personalized_pagerank(edges, {"a"})
    assert ranked[0][0] == "hub"
    assert {node for node, _ in ranked} == {"hub", "b", "c", "d"}  # seed excluded


def test_heavier_edges_pull_neighbours_closer() -> None:
    edges = [_edge("seed", "near", 1.0), _edge("seed", "far", 0.1)]
    ranked = personalized_pagerank(edges, {"seed"})
    assert [node for node, _ in ranked][:2] == ["near", "far"]


def test_unknown_seed_and_empty_graph_return_nothing() -> None:
    assert personalized_pagerank([], {"ghost"}) == []
    assert personalized_pagerank([_edge("a", "b")], {"ghost"}) == []


def test_tombstoned_edges_never_contribute() -> None:
    edges = [_edge("seed", "live", 1.0), _edge("seed", "dead", 0.0)]
    ranked = personalized_pagerank(edges, {"seed"})
    assert [node for node, _ in ranked] == ["live"]


def test_deterministic_across_runs_and_top_k() -> None:
    edges = [_edge("s", "a"), _edge("s", "b"), _edge("a", "b"), _edge("b", "c")]
    first = personalized_pagerank(edges, {"s"})
    second = personalized_pagerank(edges, {"s"})
    assert first == second
    assert personalized_pagerank(edges, {"s"}, top_k=2) == first[:2]
