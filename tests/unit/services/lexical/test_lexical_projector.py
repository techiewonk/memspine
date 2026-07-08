"""LexicalProjector: WRITE indexes, FORGET deletes, reset clears (D0.1)."""

from __future__ import annotations

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.services.lexical.base import LexicalHit
from memspine.services.lexical.projector import LexicalProjector


class FakeLexical:
    def __init__(self) -> None:
        self.docs: dict[str, str] = {}

    async def index(self, record: MemoryRecord) -> None:
        self.docs[record.record_id] = record.content

    async def index_many(self, records: list[MemoryRecord]) -> None:
        for record in records:
            await self.index(record)

    async def search(self, namespace: str, query: str, top_k: int = 8) -> list[LexicalHit]:
        return []

    async def delete(self, record_id: str) -> None:
        self.docs.pop(record_id, None)

    async def clear(self) -> None:
        self.docs.clear()

    async def close(self) -> None:
        pass


def _write(record: MemoryRecord) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.WRITE,
        namespace=record.namespace,
        payload={"record": record.model_dump(mode="json")},
    )


def rec() -> MemoryRecord:
    return MemoryRecord(
        record_id="r1",
        namespace="ns/a",
        memory_type="semantic",
        content="hello world",
        source=SourceInfo(role="user"),
    )


async def test_write_indexes_and_forget_deletes() -> None:
    store = FakeLexical()
    projector = LexicalProjector(store)
    await projector.apply(_write(rec()))
    assert store.docs == {"r1": "hello world"}

    await projector.apply(
        MemoryEvent(kind=EventKind.FORGET, namespace="ns/a", payload={"record_id": "r1"})
    )
    assert store.docs == {}


async def test_reset_clears() -> None:
    store = FakeLexical()
    projector = LexicalProjector(store)
    await projector.apply(_write(rec()))
    await projector.reset()
    assert store.docs == {}
