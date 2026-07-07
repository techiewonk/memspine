"""Catch-up and rebuild: replaying the event log into projectors (§4 step 6).

These are plain functions (anti-lock-in, D-17): the engine calls them at
startup; background runners may call them in maintenance pipelines later.
"""

from __future__ import annotations

from typing import Protocol

from memspine.config.constants import REPLAY_BATCH_SIZE
from memspine.core.events import MemoryEvent
from memspine.core.projector import Projector
from memspine.exceptions import RebuildUnavailableError

__all__ = ["ReplayableStorage", "catch_up", "rebuild"]


class ReplayableStorage(Protocol):
    """The slice of the storage port replay needs."""

    @property
    def can_rebuild(self) -> bool: ...

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
        while True:
            events = await storage.read_events(after_seq=offset, limit=REPLAY_BATCH_SIZE)
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


async def _assert_rebuildable(storage: ReplayableStorage, projector: Projector) -> None:
    """Full-history check (D-45): ephemeral never; rolling only while unpruned."""
    if not storage.can_rebuild:
        raise RebuildUnavailableError(
            "event log is ephemeral (D-45): events are not persisted, rebuild impossible"
        )
    first = await storage.read_events(after_seq=0, limit=1)
    if first:
        if first[0].seq != 1:
            raise RebuildUnavailableError(
                f"event log starts at seq {first[0].seq}, not 1 — the rolling window "
                "has pruned history (D-45); a full rebuild is no longer possible"
            )
    elif await storage.get_offset(projector.name) > 0:
        raise RebuildUnavailableError(
            "event log is empty but the projector high-water mark is advanced — "
            "history was pruned (D-45); a full rebuild is no longer possible"
        )


async def rebuild(storage: ReplayableStorage, projector: Projector) -> int:
    """Reset one projector and replay from seq 0 (D0.1 rebuild guarantee).

    Raises :class:`RebuildUnavailableError` when the log cannot serve a full
    replay — ``ephemeral`` mode, or a ``rolling`` window that already pruned
    history (D-45). ``full`` and unpruned ``rolling`` logs rebuild fine.
    """
    await _assert_rebuildable(storage, projector)
    await projector.reset()
    await storage.reset_offset(projector.name)
    counts = await catch_up(storage, [projector])
    return counts[projector.name]
