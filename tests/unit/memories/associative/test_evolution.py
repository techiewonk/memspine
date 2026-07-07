"""Bounded A-MEM evolution (D-42/ADR-015): deterministic, gated, bounded."""

from __future__ import annotations

from collections.abc import Callable

from assoc_helpers import EventLog, FakeStorage

from memspine.config.constants import EVOLUTION_LINK_MIN_SIMILARITY
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.associative.evolution import propose_links
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.vector.base import VectorHit


def _write(record: MemoryRecord) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.WRITE,
        namespace=record.namespace,
        payload={"record": record.model_dump(mode="json")},
    )


async def test_proposes_links_for_similar_neighbours_bounded(
    graph: SQLiteAdjacencyGraph,
    log: EventLog,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    fresh = storage.add(make_record("fresh"))
    neighbours = [storage.add(make_record(f"n{i}")) for i in range(6)]
    for record in (fresh, *neighbours):
        await log.append(_write(record))
    hits = [
        VectorHit(record_id=n.record_id, score=0.95 - 0.01 * i) for i, n in enumerate(neighbours)
    ]
    created = await propose_links(
        namespace="default",
        record=fresh,
        hits=hits,
        storage=storage,
        graph=graph,
        append_event=log.append,
        max_links=3,
    )
    assert created == 3  # per-write cap, not "all six"
    link_events = [e for e in log.events if e.kind is EventKind.LINK]
    assert len(link_events) == 3
    assert all(e.payload["reason"] == "evolution" for e in link_events)
    # The strongest hits win, weight = similarity.
    assert {e.payload["dst"] for e in link_events} == {n.record_id for n in neighbours[:3]}


async def test_gates_skip_unqualified_neighbours(
    graph: SQLiteAdjacencyGraph,
    log: EventLog,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    fresh = storage.add(make_record("fresh"))
    quarantined = storage.add(
        make_record("poison", quarantined=True, status=RecordStatus.QUARANTINED)
    )
    foreign = storage.add(make_record("other tenant", namespace="b"))
    archived = storage.add(make_record("old", status=RecordStatus.ARCHIVED))
    weak = storage.add(make_record("barely similar"))
    hits = [
        VectorHit(record_id=fresh.record_id, score=1.0),  # itself — skipped
        VectorHit(record_id=quarantined.record_id, score=0.9),
        VectorHit(record_id=foreign.record_id, score=0.9),
        VectorHit(record_id=archived.record_id, score=0.9),
        VectorHit(record_id="missing", score=0.9),
        VectorHit(record_id=weak.record_id, score=EVOLUTION_LINK_MIN_SIMILARITY - 0.01),
    ]
    created = await propose_links(
        namespace="default",
        record=fresh,
        hits=hits,
        storage=storage,
        graph=graph,
        append_event=log.append,
    )
    assert created == 0
    assert log.events == []


async def test_existing_partners_are_not_relinked(
    graph: SQLiteAdjacencyGraph,
    log: EventLog,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    fresh = storage.add(make_record("fresh"))
    partner = storage.add(make_record("already linked"))
    await graph.upsert_edge(fresh.record_id, partner.record_id, "related", {"weight": 0.7})
    created = await propose_links(
        namespace="default",
        record=fresh,
        hits=[VectorHit(record_id=partner.record_id, score=0.9)],
        storage=storage,
        graph=graph,
        append_event=log.append,
    )
    assert created == 0


async def test_neighbours_at_budget_are_skipped_not_errors(
    graph: SQLiteAdjacencyGraph,
    log: EventLog,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    fresh = storage.add(make_record("fresh"))
    full = storage.add(make_record("popular"))
    for i in range(12):  # LINK_BUDGET
        await graph.upsert_edge(full.record_id, f"fan-{i}", "related", {"weight": 1.0})
    created = await propose_links(
        namespace="default",
        record=fresh,
        hits=[VectorHit(record_id=full.record_id, score=0.9)],
        storage=storage,
        graph=graph,
        append_event=log.append,
    )
    assert created == 0
