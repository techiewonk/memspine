"""RecordProjector delta gate (E1): lifecycle patches only, loudly filtered."""

from __future__ import annotations

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.services.storage.projector import RecordProjector


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}

    async def upsert_record(self, record: MemoryRecord) -> None:
        self.records[record.record_id] = record

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        return self.records.get(record_id)

    async def delete_record(self, record_id: str) -> None:
        self.records.pop(record_id, None)

    async def delete_all_records(self) -> None:
        self.records.clear()


async def test_delta_may_patch_lifecycle_fields() -> None:
    store = MemoryStore()
    projector = RecordProjector(store)
    record = MemoryRecord(namespace="ns", memory_type="working", content="x")
    store.records[record.record_id] = record
    await projector.apply(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace="ns",
            actor="system",
            payload={
                "record_id": record.record_id,
                "set": {"tier": "warm", "skill_stage": None},
                "transition": "hot->warm",
                "reason": "idle",
            },
        )
    )
    assert store.records[record.record_id].tier == "warm"


async def test_illegal_delta_keys_are_dropped_not_applied() -> None:
    """A delta that tries to launder trust or rewrite content past the firewall
    is filtered: the legal keys apply, the illegal ones are ignored loudly."""
    store = MemoryStore()
    projector = RecordProjector(store)
    record = MemoryRecord(namespace="ns", memory_type="semantic", content="original", trust=0.3)
    store.records[record.record_id] = record
    await projector.apply(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace="ns",
            actor="system",
            payload={
                "record_id": record.record_id,
                "set": {"tier": "warm", "trust": 1.0, "entity": "hijacked"},
                "transition": "hot->warm",
                "reason": "idle",
            },
        )
    )
    patched = store.records[record.record_id]
    assert patched.tier == "warm"  # legal key applied
    assert patched.trust == 0.3  # illegal keys dropped
    assert patched.entity is None
    assert patched.content == "original"
