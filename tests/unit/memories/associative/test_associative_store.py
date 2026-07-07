"""AssociativeMemory (M13.6): link validation, budget, gated PPR recall."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from assoc_helpers import EventLog, FakeStorage

from memspine.config.constants import LINK_BUDGET
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConflictError
from memspine.memories.associative.links import link_event
from memspine.memories.associative.store import AssociativeMemory
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph


@pytest.fixture
def memory(storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog) -> AssociativeMemory:
    return AssociativeMemory(storage, graph, log.append)


def _write(record: MemoryRecord) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.WRITE,
        namespace=record.namespace,
        payload={"record": record.model_dump(mode="json")},
    )


async def test_link_appends_a_link_event_and_projects_the_edge(
    memory: AssociativeMemory,
    storage: FakeStorage,
    graph: SQLiteAdjacencyGraph,
    log: EventLog,
    make_record: Callable[..., MemoryRecord],
) -> None:
    a, b = storage.add(make_record("a")), storage.add(make_record("b"))
    await memory.link("default", a.record_id, b.record_id, weight=0.9)
    [event] = [e for e in log.events if e.kind is EventKind.LINK]
    assert event.payload == {
        "src": a.record_id,
        "dst": b.record_id,
        "rel": "related",
        "weight": 0.9,
        "reason": "manual",
    }
    assert await graph.edge_count() == 1


async def test_cross_namespace_link_refused_with_not_found_shape(
    memory: AssociativeMemory,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    a = storage.add(make_record("a"))
    foreign = storage.add(make_record("b", namespace="tenant-b"))
    with pytest.raises(ConflictError, match="no such record"):
        await memory.link("default", a.record_id, foreign.record_id)
    # Deleted and missing read identically (no existence oracle, ADR-014).
    gone = storage.add(make_record("gone", status=RecordStatus.DELETED))
    with pytest.raises(ConflictError, match="no such record"):
        await memory.link("default", a.record_id, gone.record_id)
    with pytest.raises(ConflictError, match="no such record"):
        await memory.link("default", a.record_id, "never-existed")


async def test_self_links_and_non_positive_weights_refused(
    memory: AssociativeMemory,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    a, b = storage.add(make_record("a")), storage.add(make_record("b"))
    with pytest.raises(ConflictError, match="itself"):
        await memory.link("default", a.record_id, a.record_id)
    with pytest.raises(ConflictError, match="positive"):
        await memory.link("default", a.record_id, b.record_id, weight=0.0)
    with pytest.raises(ConflictError, match="positive"):
        await memory.link("default", a.record_id, b.record_id, weight=float("nan"))


async def test_quarantined_endpoints_refused(
    memory: AssociativeMemory,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    clean = storage.add(make_record("clean"))
    poison = storage.add(make_record("poison", quarantined=True, status=RecordStatus.QUARANTINED))
    with pytest.raises(ConflictError, match="quarantined"):
        await memory.link("default", clean.record_id, poison.record_id)
    with pytest.raises(ConflictError, match="quarantined"):
        await memory.link("default", poison.record_id, clean.record_id)


async def test_budget_enforced_and_prune_frees_a_slot(
    memory: AssociativeMemory,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    hub = storage.add(make_record("hub"))
    spokes = [storage.add(make_record(f"s{i}")) for i in range(LINK_BUDGET + 1)]
    for spoke in spokes[:LINK_BUDGET]:
        await memory.link("default", hub.record_id, spoke.record_id, weight=0.5)
    with pytest.raises(ConflictError, match="budget"):
        await memory.link("default", hub.record_id, spokes[-1].record_id)
    assert await memory.budget_remaining(hub.record_id) == 0
    pruned = await memory.prune_weakest("default", hub.record_id)
    assert pruned is not None
    await memory.link("default", hub.record_id, spokes[-1].record_id)  # slot freed


async def test_reweighting_an_existing_edge_is_not_double_counted(
    memory: AssociativeMemory,
    storage: FakeStorage,
    graph: SQLiteAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
) -> None:
    a, b = storage.add(make_record("a")), storage.add(make_record("b"))
    await memory.link("default", a.record_id, b.record_id, weight=0.4)
    await memory.link("default", a.record_id, b.record_id, weight=0.8)
    assert await graph.edge_count() == 1
    assert await memory.budget_remaining(a.record_id) == LINK_BUDGET - 1


async def test_related_ranks_gated_namespace_truth_only(
    memory: AssociativeMemory,
    storage: FakeStorage,
    log: EventLog,
    make_record: Callable[..., MemoryRecord],
) -> None:
    seed = storage.add(make_record("seed"))
    close = storage.add(make_record("close"))
    far = storage.add(make_record("far"))
    quarantined = storage.add(
        make_record("poison", quarantined=True, status=RecordStatus.QUARANTINED)
    )
    archived = storage.add(make_record("archived", status=RecordStatus.ARCHIVED))
    await memory.link("default", seed.record_id, close.record_id, weight=1.0)
    await memory.link("default", close.record_id, far.record_id, weight=0.5)
    # Poison and archived rows joined the graph BEFORE their status changed —
    # project the edges directly (the memory layer would refuse them now).
    for other in (quarantined, archived):
        await log.append(
            link_event("default", seed.record_id, other.record_id, "related", 1.0, "test")
        )
    related = await memory.related("default", seed.record_id, k=10)
    assert [record.record_id for record in related] == [close.record_id, far.record_id]
    # M1 reinforcement: the returned ids rode a RETRIEVE event.
    retrieve = [e for e in log.events if e.kind is EventKind.RETRIEVE]
    assert retrieve and retrieve[-1].payload["record_ids"] == [
        close.record_id,
        far.record_id,
    ]


async def test_related_requires_the_seed_in_namespace(
    memory: AssociativeMemory,
    storage: FakeStorage,
    make_record: Callable[..., MemoryRecord],
) -> None:
    foreign = storage.add(make_record("theirs", namespace="tenant-b"))
    with pytest.raises(ConflictError, match="no such record"):
        await memory.related("default", foreign.record_id)
