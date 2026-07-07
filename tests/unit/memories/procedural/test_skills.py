"""Procedural store (M13.4/E6): entry stages, delta transitions, usable gate."""

from __future__ import annotations

import pytest

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConflictError
from memspine.memories.procedural.lifecycle import SkillStage
from memspine.memories.procedural.skills import (
    ProceduralMemory,
    make_skill_record,
    stage_status,
)


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


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def events() -> list[MemoryEvent]:
    return []


@pytest.fixture
def store(storage: FakeStorage, events: list[MemoryEvent]) -> ProceduralMemory:
    async def append(event: MemoryEvent) -> None:
        events.append(event)
        # Mirror the projector's delta merge so follow-up reads see the change.
        if event.kind is EventKind.DECAY_TRANSITION:
            record = storage.records[str(event.payload["record_id"])]
            data = record.model_dump()
            data.update(event.payload["set"])  # type: ignore[call-overload]
            storage.records[record.record_id] = MemoryRecord.model_validate(data)

    return ProceduralMemory(storage, append)


def test_skills_enter_at_draft_and_plans_at_staged() -> None:
    skill = make_skill_record("ns", "deploy", "how to deploy", kind="skill")
    plan = make_skill_record("ns", "release v2", "1. tag 2. push", kind="plan")
    assert skill.skill_stage == SkillStage.DRAFT.value
    assert skill.status is RecordStatus.RESOLVING
    assert plan.skill_stage == SkillStage.STAGED.value  # E6: success was step one
    assert plan.status is RecordStatus.RESOLVING
    assert skill.entity == "deploy" and skill.attribute == "skill"
    assert plan.entity == "release v2" and plan.attribute == "plan"


def test_unknown_kind_is_refused() -> None:
    with pytest.raises(ConflictError, match="unknown procedural kind"):
        make_skill_record("ns", "x", "y", kind="ritual")


def test_stage_status_mapping() -> None:
    assert stage_status(SkillStage.ACTIVE) is RecordStatus.ACTIVATED
    assert stage_status(SkillStage.DEPRECATED) is RecordStatus.ARCHIVED
    for held in (SkillStage.DRAFT, SkillStage.STAGED, SkillStage.VERIFIED):
        assert stage_status(held) is RecordStatus.RESOLVING


async def test_promotion_walks_the_ladder_as_delta_events(
    store: ProceduralMemory, storage: FakeStorage, events: list[MemoryEvent]
) -> None:
    record = make_skill_record("ns", "deploy", "steps", kind="skill")
    storage.records[record.record_id] = record

    promoted = await store.promote(record.record_id)
    assert promoted.skill_stage == SkillStage.STAGED.value
    promoted = await store.promote(record.record_id)
    assert promoted.skill_stage == SkillStage.VERIFIED.value

    with pytest.raises(ConflictError, match="dry run"):
        await store.promote(record.record_id)

    promoted = await store.promote(record.record_id, dry_run_passed=True)
    assert promoted.skill_stage == SkillStage.ACTIVE.value
    assert promoted.status is RecordStatus.ACTIVATED

    # Every transition was a delta event — never a full snapshot (P3.1 rule).
    transitions = [e for e in events if e.kind is EventKind.DECAY_TRANSITION]
    assert len(transitions) == 3
    for event in transitions:
        assert "record" not in event.payload
        assert set(event.payload) == {"record_id", "set", "transition", "reason"}


async def test_quarantined_skill_cannot_be_promoted(
    store: ProceduralMemory, storage: FakeStorage
) -> None:
    record = make_skill_record("ns", "evil", "steps", kind="skill").model_copy(
        update={"quarantined": True, "status": RecordStatus.QUARANTINED}
    )
    storage.records[record.record_id] = record
    with pytest.raises(ConflictError, match="quarantined"):
        await store.promote(record.record_id)


async def test_deprecate_is_terminal_and_idempotent(
    store: ProceduralMemory, storage: FakeStorage, events: list[MemoryEvent]
) -> None:
    record = make_skill_record("ns", "old", "steps", kind="skill")
    storage.records[record.record_id] = record
    retired = await store.deprecate(record.record_id)
    assert retired.skill_stage == SkillStage.DEPRECATED.value
    assert retired.status is RecordStatus.ARCHIVED
    assert retired.valid_to is not None
    count = len(events)
    again = await store.deprecate(record.record_id)  # idempotent, no new event
    assert again.skill_stage == SkillStage.DEPRECATED.value
    assert len(events) == count
    with pytest.raises(ConflictError, match="terminal"):
        await store.promote(record.record_id, dry_run_passed=True)


async def test_usable_gate_filters_stage_status_and_quarantine(
    store: ProceduralMemory, storage: FakeStorage
) -> None:
    draft = make_skill_record("ns", "a", "x", kind="skill")
    active = make_skill_record("ns", "b", "y", kind="skill").model_copy(
        update={"skill_stage": SkillStage.ACTIVE.value, "status": RecordStatus.ACTIVATED}
    )
    poisoned = active.model_copy(update={"record_id": "poisoned", "quarantined": True})
    for record in (draft, active, poisoned):
        storage.records[record.record_id] = record
    usable = await store.list("ns", usable_only=True)
    assert [r.record_id for r in usable] == [active.record_id]


async def test_non_procedural_record_is_refused(
    store: ProceduralMemory, storage: FakeStorage
) -> None:
    stray = MemoryRecord(namespace="ns", memory_type="semantic", content="fact")
    storage.records[stray.record_id] = stray
    with pytest.raises(ConflictError, match="not procedural"):
        await store.promote(stray.record_id)
    with pytest.raises(ConflictError, match="no such record"):
        await store.promote("missing")
