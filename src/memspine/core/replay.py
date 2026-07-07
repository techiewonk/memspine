"""Catch-up and rebuild: replaying the event log into projectors (§4 step 6).

These are plain functions (anti-lock-in, D-17): the engine calls them at
startup; background runners may call them in maintenance pipelines later.
"""

from __future__ import annotations

from typing import Protocol

from memspine.core.events import MemoryEvent
from memspine.core.projector import Projector
from memspine.exceptions import RebuildUnavailableError

__all__ = ["ReplayableStorage", "catch_up", "rebuild"]

_BATCH = 1000


class ReplayableStorage(Protocol):
    """The slice of the storage port replay needs."""

    @property
    def can_rebuild(self) -> bool: ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...

    async def get_offset(self, projector_name: str) -> int: ...

    async def set_offset(self, projector_name: str, seq: int) -> None: ...


async def catch_up(storage: ReplayableStorage, projectors: list[Projector]) -> dict[str, int]:
    """Replay events past each projector's high-water mark. Returns applied counts."""
    applied: dict[str, int] = {}
    for projector in projectors:
        offset = await storage.get_offset(projector.name)
        count = 0
        while True:
            events = await storage.read_events(after_seq=offset, limit=_BATCH)
            if not events:
                break
            for event in events:
                await projector.apply(event)
                assert event.seq is not None  # events out of the log always carry seq
                offset = event.seq
                count += 1
            await storage.set_offset(projector.name, offset)
        applied[projector.name] = count
    return applied


async def rebuild(storage: ReplayableStorage, projector: Projector) -> int:
    """Reset one projector and replay from seq 0 (D0.1 rebuild guarantee).

    Raises :class:`RebuildUnavailableError` when the log cannot serve a full
    replay — ``ephemeral`` mode, or a ``rolling`` window that already pruned
    history (D-45).
    """
    if not storage.can_rebuild:
        raise RebuildUnavailableError(
            "event log cannot serve a full rebuild in this event_log.mode (D-45); "
            "use mode=full to retain rebuildability"
        )
    await projector.reset()
    await storage.set_offset(projector.name, 0)
    counts = await catch_up(storage, [projector])
    return counts[projector.name]
