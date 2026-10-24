"""GraphProjector: WRITE→node, derivation→derived_from, LINK→edge, FORGET→cascade."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from memspine.derive.gliner_mentions import bind_gliner_mentions, parse_gliner_mentions
from memspine.memories.associative.links import link_event
from memspine.memories.associative.projector import GraphProjector
from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord


async def test_write_projects_one_node_per_record(
    graph: SqlAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    for content in ("first", "second"):
        await projector.apply(write_event(make_record(content)))
    assert await graph.node_count() == 2
    assert await graph.edge_count() == 0


async def test_consolidation_and_reflection_payloads_project_derived_from(
    graph: SqlAdjacencyGraph,
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
    graph: SqlAdjacencyGraph, projector: GraphProjector
) -> None:
    event = link_event("default", "a", "b", "related", weight=0.8, reason="manual")
    await projector.apply(event)
    await projector.apply(event)  # catch-up re-delivery must be safe
    edges = await graph.edge_list()
    assert len(edges) == 1
    assert edges[0].weight == 0.8
    assert edges[0].properties["reason"] == "manual"


async def test_forget_deletes_the_node_and_every_touching_edge(
    graph: SqlAdjacencyGraph,
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
    graph: SqlAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    await projector.apply(write_event(make_record("gone soon")))
    await projector.reset()
    assert await graph.node_count() == 0
    assert await graph.edge_count() == 0


def _annotated(record: MemoryRecord, spans: list[tuple[int, int]]) -> MemoryRecord:
    raw = {
        "person": [
            {"text": record.content[start:end], "start": start, "end": end} for start, end in spans
        ]
    }
    mentions = bind_gliner_mentions(
        record,
        parse_gliner_mentions(record.content, raw, ["person"]),
    )
    return record.model_copy(
        update={"fingerprint": record.fingerprint.model_copy(update={"entity_mentions": mentions})}
    )


async def test_bound_mentions_project_distinct_unasserted_occurrences(
    graph: SqlAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    record = _annotated(make_record("Mira met Mira", namespace="tenant-a"), [(0, 4), (9, 13)])

    await projector.apply(write_event(record))
    await projector.apply(write_event(record))

    edges = await graph.edge_list("tenant-a")
    source_edges = [edge for edge in edges if edge.rel_type == "has_mention"]
    reference_edges = [edge for edge in edges if edge.rel_type == "refers_to"]
    assert len(source_edges) == len(reference_edges) == 2
    assert len({edge.dst for edge in source_edges}) == 2
    assert len({edge.dst for edge in reference_edges}) == 1
    assert all(edge.properties["asserted"] is False for edge in edges)
    assert {edge.properties["start"] for edge in source_edges} == {0, 9}


async def test_reannotation_replaces_stale_occurrences_and_empty_is_success(
    graph: SqlAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    original = _annotated(make_record("Mira arrived", namespace="tenant-a"), [(0, 4)])
    await projector.apply(write_event(original))
    assert len([e for e in await graph.edge_list() if e.rel_type == "has_mention"]) == 1

    cleared = original.model_copy(
        update={"fingerprint": original.fingerprint.model_copy(update={"entity_mentions": ()})}
    )
    await projector.apply(write_event(cleared))

    assert not [e for e in await graph.edge_list() if e.rel_type in {"has_mention", "refers_to"}]
    assert await graph.node_count() == 1


async def test_forget_removes_occurrences_and_surfaces(
    graph: SqlAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    record = _annotated(make_record("Mira arrived", namespace="tenant-a"), [(0, 4)])
    await projector.apply(write_event(record))

    await projector.apply(
        MemoryEvent(
            kind=EventKind.FORGET,
            namespace=record.namespace,
            payload={"record_id": record.record_id},
        )
    )

    assert await graph.node_count() == 0
    assert await graph.edge_count() == 0


async def test_projector_refuses_forged_or_stale_mention_binding(
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    record = _annotated(make_record("Mira arrived", namespace="tenant-a"), [(0, 4)])
    (mention,) = record.fingerprint.entity_mentions
    forged = record.model_copy(
        update={
            "fingerprint": record.fingerprint.model_copy(
                update={"entity_mentions": (mention.model_copy(update={"sender_id": "mallory"}),)}
            )
        }
    )

    with pytest.raises(ValueError, match="current source binding"):
        await projector.apply(write_event(forged))
