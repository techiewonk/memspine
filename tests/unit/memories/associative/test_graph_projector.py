"""GraphProjector: WRITE→node, derivation→derived_from, LINK→edge, FORGET→cascade."""

from __future__ import annotations

from collections.abc import Callable

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.associative.links import link_event
from memspine.memories.associative.projector import GraphProjector
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph


async def test_write_projects_one_node_per_record(
    graph: SQLiteAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    for content in ("first", "second"):
        await projector.apply(write_event(make_record(content)))
    assert await graph.node_count() == 2
    assert await graph.edge_count() == 0


async def test_consolidation_and_reflection_payloads_project_derived_from(
    graph: SQLiteAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    members = [make_record(f"episode {i}", memory_type="episodic") for i in range(2)]
    for member in members:
        await projector.apply(write_event(member))
    summary = make_record("a summary")
    await projector.apply(
        write_event(
            summary,
            consolidation={"member_record_ids": [m.record_id for m in members]},
        )
    )
    edges = await graph.edge_list()
    assert {(e.src, e.dst, e.rel_type) for e in edges} == {
        (summary.record_id, m.record_id, "derived_from") for m in members
    }
    reflection = make_record("an insight", memory_type="reflective")
    await projector.apply(
        write_event(reflection, reflection={"member_record_ids": [members[0].record_id]})
    )
    assert (
        reflection.record_id,
        members[0].record_id,
        "derived_from",
    ) in {(e.src, e.dst, e.rel_type) for e in await graph.edge_list()}


async def test_link_events_project_weighted_edges_idempotently(
    graph: SQLiteAdjacencyGraph, projector: GraphProjector
) -> None:
    event = link_event("default", "a", "b", "related", weight=0.8, reason="manual")
    await projector.apply(event)
    await projector.apply(event)  # catch-up re-delivery must be safe
    edges = await graph.edge_list()
    assert len(edges) == 1
    assert edges[0].weight == 0.8
    assert edges[0].properties["reason"] == "manual"


async def test_forget_deletes_the_node_and_every_touching_edge(
    graph: SQLiteAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    a, b = make_record("alpha"), make_record("beta")
    for record in (a, b):
        await projector.apply(write_event(record))
    await projector.apply(link_event("default", a.record_id, b.record_id, "related", 1.0, "x"))
    await projector.apply(
        MemoryEvent(kind=EventKind.FORGET, namespace="default", payload={"record_id": b.record_id})
    )
    assert await graph.edge_count() == 0
    assert await graph.node_count() == 1


async def test_reset_clears_the_projection_for_rebuild(
    graph: SQLiteAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    await projector.apply(write_event(make_record("gone soon")))
    await projector.reset()
    assert await graph.node_count() == 0
    assert await graph.edge_count() == 0
