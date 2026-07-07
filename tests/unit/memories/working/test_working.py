"""Working memory (M13.1): paging selection, persona pinning, manager events."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.working.manager import WorkingMemory
from memspine.memories.working.paging import select_page_out
from memspine.memories.working.persona import is_persona, make_persona_record


def working_record(content: str, age_minutes: int = 0) -> MemoryRecord:
    return MemoryRecord(
        namespace="agent/a",
        memory_type="working",
        content=content,
        recorded_at=datetime.now(UTC) - timedelta(minutes=age_minutes),
    )


def test_select_page_out_evicts_oldest_beyond_capacity() -> None:
    newest = working_record("new", age_minutes=0)
    middle = working_record("mid", age_minutes=5)
    oldest = working_record("old", age_minutes=10)
    evicted = select_page_out([newest, oldest, middle], page_size=2)
    assert [record.content for record in evicted] == ["old"]


def test_persona_is_pinned_never_paged() -> None:
    persona = make_persona_record("agent/a", "I am the assistant")
    assert is_persona(persona)
    assert persona.scoring.importance == 1.0
    turns = [working_record(f"t{i}", age_minutes=i) for i in range(3)]
    evicted = select_page_out([persona, *turns], page_size=2)
    assert persona not in evicted
    assert len(evicted) == 1  # only unpinned overflow counts against page_size


async def test_manager_emits_decay_transitions_through_the_door() -> None:
    emitted: list[MemoryEvent] = []

    async def capture(event: MemoryEvent) -> None:
        emitted.append(event)

    manager = WorkingMemory(append_event=capture, page_size=1)
    old, new = working_record("old", 10), working_record("new", 0)
    evicted = await manager.enforce("agent/a", [old, new])

    assert [record.content for record in evicted] == ["old"]
    assert len(emitted) == 1
    event = emitted[0]
    assert event.kind is EventKind.DECAY_TRANSITION
    assert event.payload["transition"] == "working->episodic"
    transitioned = event.payload["record"]
    assert transitioned["record_id"] == old.record_id  # identity preserved
    assert transitioned["memory_type"] == "episodic"
    assert transitioned["version"] == old.version + 1
