"""Shared harness: a real SQLite-adjacency graph, an event-applying append
seam (events land in the projector exactly like the engine's write door), and
a storage fake exposing only the ``get_record`` slice associative declares."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from assoc_helpers import EventLog, FakeStorage

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.associative.projector import GraphProjector
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.storage.sqlite.schema import metadata


@pytest.fixture
async def graph() -> AsyncIterator[SQLiteAdjacencyGraph]:
    client = SQLiteClient(":memory:")
    await client.connect()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield SQLiteAdjacencyGraph(client)
    await client.close()


@pytest.fixture
async def projector(graph: SQLiteAdjacencyGraph) -> GraphProjector:
    return GraphProjector(graph)


@pytest.fixture
async def log(projector: GraphProjector) -> EventLog:
    return EventLog(projector)


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def make_record() -> Callable[..., MemoryRecord]:
    def _make(
        content: str = "a plain fact",
        namespace: str = "default",
        memory_type: str = "semantic",
        **kwargs: Any,
    ) -> MemoryRecord:
        return MemoryRecord(namespace=namespace, memory_type=memory_type, content=content, **kwargs)

    return _make


@pytest.fixture
def write_event() -> Callable[..., MemoryEvent]:
    def _event(record: MemoryRecord, **extra_payload: Any) -> MemoryEvent:
        return MemoryEvent(
            kind=EventKind.WRITE,
            namespace=record.namespace,
            payload={"record": record.model_dump(mode="json"), **extra_payload},
        )

    return _event
