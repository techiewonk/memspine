"""#22 (``read.graph_rerank``): node-distance / local push-PPR boost from the graph
leg's seeds, plus the episode-mentions boost. Off is byte-identical (the
``test_graph_leg_off_golden`` snapshot covers the explicit off values)."""

from __future__ import annotations

from typing import Any

import pytest
from test_graph_leg_off_golden import _engine, seed

from memspine import Engine
from memspine.config import constants
from memspine.core.records import MemoryRecord
from memspine.memories.associative.ppr import local_push_ppr, personalized_pagerank
from memspine.services.graph.base import GraphEdge

QUERY = "what books has Melanie read"
MELANIE = {'Melanie read "Charlotte\'s Web"', 'Melanie read "Nothing Is Impossible"'}


async def _started(**read: Any) -> Engine:
    eng = _engine(**read)
    await eng.start()
    return eng


async def _flat(eng: Engine) -> list[tuple[MemoryRecord, float]]:
    """Every record of the fixture at one equal relevance, in a fixed order."""
    storage = eng._require_started()
    records = sorted(await storage.list_records("a"), key=lambda r: r.content)
    return [(record, 0.5) for record in records]


@pytest.mark.parametrize("mode", ["distance", "ppr"])
async def test_records_near_the_query_entities_rise(mode: str) -> None:
    eng = await _started(graph_rerank=mode)
    try:
        await seed(eng)
        ranked = await eng._graph_rerank("a", QUERY, await _flat(eng))
        top = {r.content for r, _ in ranked[: len(MELANIE)]}
        assert top == MELANIE
        scores = {r.content: s for r, s in ranked}
        assert all(scores[c] > 0.5 for c in MELANIE)
        # Records off the seeds' walk keep their relevance exactly.
        assert scores["the weekend hike got cancelled"] == 0.5
        assert all(0.0 <= s <= 1.0 for s in scores.values())
    finally:
        await eng.stop()


async def test_distance_boost_decays_with_entity_hops() -> None:
    eng = await _started(graph_rerank="distance", graph_depth=2)
    try:
        await seed(eng)
        assert eng._associative is not None
        seeds = await eng._graph_seeds("a", "tell me about Biscuit")
        proximity = await eng._associative.graph_proximity(
            "a", seeds, depth=2, admit=eng._graph_admit("a"), mode="distance"
        )
        storage = eng._require_started()
        by_content = {
            (await storage.get_record(rid)).content: score  # type: ignore[union-attr]
            for rid, score in proximity.items()
        }
        assert by_content["Caroline owns a dog named Biscuit"] == 1.0
        assert by_content["Caroline lives in Denver"] == 0.5  # one entity further
    finally:
        await eng.stop()


async def test_weight_zero_and_off_change_nothing() -> None:
    eng = await _started(graph_rerank="ppr", graph_rerank_weight=0.0)
    try:
        await seed(eng)
        flat = await _flat(eng)
        assert await eng._graph_rerank("a", QUERY, flat) == flat
    finally:
        await eng.stop()


async def test_episode_mentions_lift_restated_facts() -> None:
    eng = await _started(graph_rerank="distance")
    try:
        ids = await seed(eng)
        storage = eng._require_started()
        plain = await storage.get_record(ids["Caroline lives in Denver"])
        restated = await storage.get_record(ids["Caroline owns a dog named Biscuit"])
        assert plain is not None and restated is not None
        prefix = constants.EDGE_SOURCE_TAG_PREFIX
        restated = restated.model_copy(
            update={"tags": [*restated.tags, f"{prefix}x1", f"{prefix}x2"]}
        )
        # A query naming no entity and no graph walk: only the mentions boost acts.
        eng._associative = None
        ranked = await eng._graph_rerank("a", "anything", [(plain, 0.4), (restated, 0.4)])
        assert [r.record_id for r, _ in ranked] == [restated.record_id, plain.record_id]
        assert ranked[1][1] == 0.4
    finally:
        await eng.stop()


async def test_read_runs_end_to_end_with_the_rerank_on() -> None:
    for mode in ("distance", "ppr"):
        eng = await _started(graph_rerank=mode, graph_leg=True)
        try:
            await seed(eng)
            result = await eng.read(QUERY, namespace="a", mode="retrieve", top_k=4)
            assert MELANIE & {r.content for r in result.context.records}
        finally:
            await eng.stop()


def _edges(*pairs: tuple[str, str, float]) -> list[GraphEdge]:
    return [GraphEdge(a, b, "related", {"weight": w}) for a, b, w in pairs]


def test_local_push_ppr_agrees_with_power_iteration_on_order() -> None:
    edges = _edges(
        ("s", "a", 1.0),
        ("a", "b", 1.0),
        ("b", "c", 1.0),
        ("s", "d", 0.2),
        ("d", "c", 0.2),
        ("x", "y", 1.0),  # another component: never reached
    )
    local = local_push_ppr(edges, {"s"}, epsilon=1e-7)
    assert "x" not in local and "y" not in local
    ranked_local = [n for n, _ in sorted(local.items(), key=lambda p: (-p[1], p[0])) if n != "s"]
    ranked_power = [n for n, _ in personalized_pagerank(edges, {"s"}, iterations=20)]
    assert ranked_local == [n for n in ranked_power if n in local]
    assert local == local_push_ppr(list(reversed(edges)), {"s"}, epsilon=1e-7)  # order-free


def test_local_push_ppr_without_live_seeds_is_empty() -> None:
    assert local_push_ppr(_edges(("a", "b", 1.0)), {"zz"}) == {}
    assert local_push_ppr(_edges(("a", "b", 0.0)), {"a"}) == {}
