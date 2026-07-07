"""Replay/rebuild regression tests (D0.1 + D-45 window semantics)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.replay import catch_up, rebuild
from memspine.exceptions import RebuildUnavailableError
from memspine.services.storage.sqlite.engine import SQLiteStorage


class DictProjector(Projector):
    def __init__(self, name: str = "dict") -> None:
        self.name = name
        self.state: dict[str, str] = {}

    async def apply(self, event: MemoryEvent) -> None:
        self.state[event.event_id] = event.namespace

    async def reset(self) -> None:
        self.state.clear()


async def make_storage(mode: EventLogMode = EventLogMode.FULL) -> SQLiteStorage:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client, mode=mode)
    await storage.start()
    return storage


def ev(**payload: object) -> MemoryEvent:
    return MemoryEvent(kind=EventKind.WRITE, namespace="ns/a", payload=dict(payload))


async def test_unpruned_rolling_log_can_rebuild() -> None:
    """Regression: can_rebuild used to be False for rolling even with full history."""
    storage = await make_storage(EventLogMode.ROLLING)
    projector = DictProjector()
    for i in range(3):
        await storage.append_event(ev(i=i))
    await catch_up(storage, [projector])

    count = await rebuild(storage, projector)  # nothing pruned -> full replay works
    assert count == 3


async def test_pruned_rolling_log_refuses_rebuild() -> None:
    storage = await make_storage(EventLogMode.ROLLING)
    projector = DictProjector()
    for i in range(3):
        await storage.append_event(ev(i=i))
    await catch_up(storage, [projector])
    pruned = await storage.prune_events(datetime.now(UTC) + timedelta(days=1))
    assert pruned == 3

    with pytest.raises(RebuildUnavailableError, match="pruned"):
        await rebuild(storage, projector)


async def test_seq_never_reused_after_prune() -> None:
    """Regression: without sqlite AUTOINCREMENT, seq restarted at 1 after a
    full prune, falling below high-water marks -> events silently skipped."""
    storage = await make_storage(EventLogMode.ROLLING)
    projector = DictProjector()
    for i in range(3):
        await storage.append_event(ev(i=i))
    await catch_up(storage, [projector])  # offset = 3
    await storage.prune_events(datetime.now(UTC) + timedelta(days=1))  # log empty

    appended = await storage.append_event(ev(i=99))
    assert appended.seq == 4  # continues past pruned history, never reuses 1

    applied = await catch_up(storage, [projector])
    assert applied["dict"] == 1  # the new event is visible to the projector


async def test_set_offset_is_advance_only() -> None:
    """Regression: interleaved checkpoints must never move a mark backwards."""
    storage = await make_storage()
    await storage.set_offset("p", 5)
    await storage.set_offset("p", 3)  # late, out-of-order checkpoint
    assert await storage.get_offset("p") == 5
    await storage.reset_offset("p")  # rebuild path may still reset
    assert await storage.get_offset("p") == 0
