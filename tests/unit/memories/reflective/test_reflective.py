"""Reflective memory (M13.7): depth cap, laundering refusal, derivation trail."""

from __future__ import annotations

import pytest

from memspine.config.constants import REFLECTION_DEPTH_CAP
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConflictError
from memspine.memories.reflective.guards import reflection_depth_for
from memspine.memories.reflective.reflections import ReflectiveMemory


def _episode(depth: int = 0, **updates: object) -> MemoryRecord:
    record = MemoryRecord(
        namespace="ns",
        memory_type="episodic" if depth == 0 else "reflective",
        content=f"depth-{depth} content",
        reflection_depth=depth,
    )
    return record.model_copy(update=updates) if updates else record


# ── guards ────────────────────────────────────────────────────────────────────


def test_depth_is_one_over_the_deepest_parent() -> None:
    assert reflection_depth_for([_episode(0)]) == 1
    assert reflection_depth_for([_episode(0), _episode(1)]) == 2


def test_depth_cap_is_terminal() -> None:
    assert REFLECTION_DEPTH_CAP == 2
    with pytest.raises(ConflictError, match="cap"):
        reflection_depth_for([_episode(REFLECTION_DEPTH_CAP)])


def test_empty_parents_are_refused() -> None:
    with pytest.raises(ConflictError, match="at least one"):
        reflection_depth_for([])


def test_quarantined_and_deleted_parents_are_refused() -> None:
    with pytest.raises(ConflictError, match="launder"):
        reflection_depth_for([_episode(0, quarantined=True)])
    with pytest.raises(ConflictError, match="launder"):
        reflection_depth_for([_episode(0, status=RecordStatus.QUARANTINED)])
    with pytest.raises(ConflictError, match="launder"):
        reflection_depth_for([_episode(0, status=RecordStatus.DELETED)])


# ── store ─────────────────────────────────────────────────────────────────────


class FakeStorage:
    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]:
        return [
            r
            for r in self.records.values()
            if r.namespace == namespace and (memory_type is None or r.memory_type == memory_type)
        ]

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        return self.records.get(record_id)


async def test_reflect_writes_depth_and_derivation_provenance() -> None:
    storage = FakeStorage()
    events: list[MemoryEvent] = []

    async def append(event: MemoryEvent) -> None:
        events.append(event)

    parent = _episode(0)
    storage.records[parent.record_id] = parent
    store = ReflectiveMemory(storage, append)

    reflection = await store.reflect("ns", "user prefers terse answers", [parent.record_id])
    assert reflection.reflection_depth == 1
    assert reflection.memory_type == "reflective"

    [event] = events
    assert event.kind is EventKind.WRITE
    assert event.payload["reflection"] == {"member_record_ids": [parent.record_id]}


async def test_reflect_refuses_missing_parent_and_uses_fetched_depths() -> None:
    storage = FakeStorage()

    async def append(event: MemoryEvent) -> None:  # pragma: no cover - never reached
        raise AssertionError("no event should be appended")

    store = ReflectiveMemory(storage, append)
    with pytest.raises(ConflictError, match="does not exist"):
        await store.reflect("ns", "insight", ["ghost"])

    # A depth-2 parent in storage caps the chain no matter what a caller claims.
    meta = _episode(2)
    storage.records[meta.record_id] = meta
    with pytest.raises(ConflictError, match="cap"):
        await store.reflect("ns", "meta-meta", [meta.record_id])


async def test_reflect_passes_the_firewall_gate_when_injected() -> None:
    storage = FakeStorage()
    events: list[MemoryEvent] = []
    assessed: list[str] = []

    async def append(event: MemoryEvent) -> None:
        events.append(event)

    async def assess(record: MemoryRecord) -> MemoryRecord:
        assessed.append(record.record_id)
        return record

    parent = _episode(0)
    storage.records[parent.record_id] = parent
    store = ReflectiveMemory(storage, append, assess=assess)
    reflection = await store.reflect("ns", "insight", [parent.record_id])
    assert assessed == [reflection.record_id]


async def test_reflections_read_filters_by_depth() -> None:
    storage = FakeStorage()

    async def append(event: MemoryEvent) -> None:  # pragma: no cover - unused
        pass

    r1 = _episode(1)
    r2 = _episode(2)
    storage.records[r1.record_id] = r1
    storage.records[r2.record_id] = r2
    store = ReflectiveMemory(storage, append)
    assert {r.record_id for r in await store.reflections("ns")} == {r1.record_id, r2.record_id}
    assert [r.record_id for r in await store.reflections("ns", depth=2)] == [r2.record_id]
