"""Link budget (M13.6/ADR-015): refusal, tombstone pruning, provenance exemption."""

from __future__ import annotations

import pytest
from assoc_helpers import EventLog

from memspine.config.constants import LINK_BUDGET
from memspine.exceptions import ConflictError
from memspine.memories.associative.links import (
    assert_within_budget,
    link_event,
    live_links,
    prune_weakest,
)
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph


async def _fill_hub(log: EventLog, count: int) -> None:
    for i in range(count):
        await log.append(
            link_event(
                "default", "hub", f"spoke-{i:02d}", "related", weight=0.1 * (i + 1), reason="t"
            )
        )


async def test_budget_refuses_the_link_past_the_cap(
    graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    await _fill_hub(log, LINK_BUDGET)
    with pytest.raises(ConflictError, match="prune_weakest"):
        await assert_within_budget(graph, "hub")
    # Every spoke carries one link — far under budget.
    await assert_within_budget(graph, "spoke-00")


async def test_prune_weakest_emits_a_weight_zero_tombstone(
    graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    await _fill_hub(log, LINK_BUDGET)
    pruned = await prune_weakest(graph, log.append, "default", "hub")
    assert pruned is not None
    assert pruned.dst == "spoke-00"  # deterministically the lowest weight
    tombstone = log.events[-1]
    assert tombstone.payload["weight"] == 0.0
    assert tombstone.payload["reason"] == "budget_prune"
    # The slot is free again: the tombstoned edge no longer counts.
    assert len(live_links(await graph.edges_of("hub"))) == LINK_BUDGET - 1
    await assert_within_budget(graph, "hub")


async def test_prune_weakest_on_a_bare_node_is_none(
    graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    assert await prune_weakest(graph, log.append, "default", "loner") is None
    assert log.events == []


async def test_provenance_edges_are_budget_exempt(
    graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    for i in range(LINK_BUDGET + 3):
        await graph.upsert_edge("summary", f"member-{i}", "derived_from", {"weight": 1.0})
    assert live_links(await graph.edges_of("summary")) == []
    await assert_within_budget(graph, "summary")  # does not raise


async def test_community_edges_are_budget_exempt(
    graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    """H1(a): reorganize membership links (rel=community) never count against
    the budget — a 20-member community keeps all 20 links (ADR-015 §2)."""
    for i in range(LINK_BUDGET + 5):
        await graph.upsert_edge(f"member-{i}", "parent", "community", {"weight": 1.0})
    assert live_links(await graph.edges_of("parent")) == []
    await assert_within_budget(graph, "parent")  # does not raise


async def test_prune_never_selects_a_reserved_edge(
    graph: SQLiteAdjacencyGraph, log: EventLog
) -> None:
    """H1(c): community/derived_from edges are never prunable — the weakest
    *associative* edge goes, and a node with only reserved edges prunes None."""
    await graph.upsert_edge("hub", "parent", "community", {"weight": 0.01})
    await graph.upsert_edge("hub", "summary", "derived_from", {"weight": 0.01})
    await graph.upsert_edge("hub", "friend", "related", {"weight": 0.9})
    pruned = await prune_weakest(graph, log.append, "default", "hub")
    assert pruned is not None
    assert (pruned.dst, pruned.rel_type) == ("friend", "related")  # never the reserved ones
    # Only reserved edges remain: nothing is eligible.
    assert await prune_weakest(graph, log.append, "default", "hub") is None
