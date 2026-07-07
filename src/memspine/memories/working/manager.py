"""Working-memory manager (M13.1): enforce the hot window through the write door.

Page-outs are DECAY_TRANSITION events (working → episodic), never direct table
writes — the log stays the single source of truth, and a rebuild reproduces
the same paging history.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.base import BaseMemory
from memspine.memories.working.paging import select_page_out

__all__ = ["WorkingMemory"]

#: The engine hands the manager this append-through-the-write-door callable.
AppendEvent = Callable[[MemoryEvent], Awaitable[None]]

DEFAULT_PAGE_SIZE = 16


class WorkingMemory(BaseMemory):
    name: ClassVar[str] = "working"

    def __init__(self, append_event: AppendEvent, page_size: int = DEFAULT_PAGE_SIZE) -> None:
        self._append_event = append_event
        self._page_size = page_size

    @property
    def page_size(self) -> int:
        return self._page_size

    async def enforce(self, namespace: str, active: list[MemoryRecord]) -> list[MemoryRecord]:
        """Page out overflow: emit one DECAY_TRANSITION per evicted record.

        The transitioned record keeps its identity (same ``record_id``) but
        becomes episodic with a bumped version — MemGPT-style spill to the
        archival tier, fully replayable from the log.
        """
        evicted = select_page_out(active, self._page_size)
        for record in evicted:
            transitioned = record.model_copy(
                update={"memory_type": "episodic", "version": record.version + 1}
            )
            await self._append_event(
                MemoryEvent(
                    kind=EventKind.DECAY_TRANSITION,
                    namespace=namespace,
                    actor="system",
                    payload={
                        "record": transitioned.model_dump(mode="json"),
                        "transition": "working->episodic",
                        "reason": "page_out",
                    },
                )
            )
        return evicted
