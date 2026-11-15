"""Catch-up and rebuild: replaying the event log into projectors (§4 step 6).

These are plain functions (anti-lock-in, D-17): the engine calls them at
startup; background runners may call them in maintenance pipelines later.
"""

from __future__ import annotations

from typing import Protocol

from memspine.config.constants import REPLAY_BATCH_SIZE
from memspine.memories.associative.entity_index import EntityIndexProjectionIncomplete
from memspine.store.events import MemoryEvent
from memspine.store.projections.projector import Projector

__all__ = ["ReplayableStorage", "catch_up", "rebuild"]


class ReplayableStorage(Protocol):
    """The slice of the storage port replay needs."""

    @property
    def can_rebuild(self) -> bool: ...

    async def assert_full_event_history(self) -> None: ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...

    async def get_offset(self, projector_name: str) -> int: ...

    async def set_offset(self, projector_name: str, seq: int) -> None: ...

    async def reset_offset(self, projector_name: str) -> None: ...


async def catch_up(storage: ReplayableStorage, projectors: list[Projector]) -> dict[str, int]:
    """Replay events past each projector's high-water mark. Returns applied counts."""
    applied: dict[str, int] = {}
    for projector in projectors:
        offset = await storage.get_offset(projector.name)
        count = 0
        if projector.name == "entity_index":
            # The durable global offset is a conservative certified prefix. A
            # pending positive marker can keep it behind while a later FORGET
            # must still be delivered to resolve that pending marker. This
            # local scan position never becomes a durable acknowledgement.
            scan_after = offset
            while True:
                events = await storage.read_events(after_seq=scan_after, limit=REPLAY_BATCH_SIZE)
                if not events:
                    break
                for event in events:
                    try:
                        await projector.apply(event)
                        count += 1
                    except EntityIndexProjectionIncomplete:
                        pass
                    assert event.seq is not None
                    scan_after = event.seq
            applied[projector.name] = count
            continue
        while True:
            events = await storage.read_events(after_seq=offset, limit=REPLAY_BATCH_SIZE)
            if not events:
                break
            for event in events:
                await projector.apply(event)
                assert event.seq is not None  # events out of the log always carry seq
                offset = event.seq
                count += 1
            if projector.name != "records":
                # The native record fold commits its own cursor with the rows.
                await storage.set_offset(projector.name, offset)
        applied[projector.name] = count
    return applied


async def _assert_rebuildable(storage: ReplayableStorage, projector: Projector) -> None:
    """Full-history check (D-45): ephemeral never; rolling only while unpruned."""
    await storage.assert_full_event_history()


async def rebuild(storage: ReplayableStorage, projector: Projector) -> int:
    """Reset one projector and replay from seq 0 (D0.1 rebuild guarantee).

    Raises :class:`RebuildUnavailableError` when the log cannot serve a full
    replay — ``ephemeral`` mode, or a ``rolling`` window that already pruned
    history (D-45). ``full`` and unpruned ``rolling`` logs rebuild fine.
    """
    await _assert_rebuildable(storage, projector)
    await projector.reset()
    if projector.name not in {"entity_index", "records"}:
        await storage.reset_offset(projector.name)
    counts = await catch_up(storage, [projector])
    return counts[projector.name]
