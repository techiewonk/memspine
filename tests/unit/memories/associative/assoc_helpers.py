"""Shared test doubles for the associative suite (imported by name — the test
directory rides sys.path, and this filename is unique repo-wide)."""

from __future__ import annotations

from memspine.core.events import MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.associative.projector import GraphProjector

__all__ = ["EventLog", "FakeStorage"]


class FakeStorage:
    """The AssociativeStore/EvolutionStore slice over an in-memory dict."""

    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}

    def add(self, record: MemoryRecord) -> MemoryRecord:
        self.records[record.record_id] = record
        return record

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        return self.records.get(record_id)


class EventLog:
    """Captures appended events AND applies them to the projector — the same
    append-then-project unit the engine's write door performs."""

    def __init__(self, projector: GraphProjector) -> None:
        self._projector = projector
        self.events: list[MemoryEvent] = []

    async def append(self, event: MemoryEvent) -> None:
        self.events.append(event)
        await self._projector.apply(event)
