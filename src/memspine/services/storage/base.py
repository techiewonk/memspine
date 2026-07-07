"""Storage port protocol (D-36): the single write door + read models.

All state mutations flow through :meth:`StorageService.append_event`. Everything
else in the system is a projection of that log (D0.1). ``upsert_record`` /
``get_record`` back the relational read model that projectors maintain.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol, runtime_checkable

from memspine.core.events import MemoryEvent
from memspine.core.records import MemoryRecord

__all__ = ["StorageService"]


@runtime_checkable
class StorageService(Protocol):
    """Relational + event-log persistence contract."""

    @property
    def can_rebuild(self) -> bool:
        """False when the event log cannot serve a full replay (D-45)."""
        ...

    async def append_event(self, event: MemoryEvent) -> MemoryEvent:
        """The single write door: assign ``seq`` and persist (mode-dependent, D-45)."""
        ...

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]: ...

    async def get_offset(self, projector_name: str) -> int: ...

    async def set_offset(self, projector_name: str, seq: int) -> None:
        """Advance-only checkpoint; implementations must never move a mark backwards."""
        ...

    async def reset_offset(self, projector_name: str) -> None:
        """Rebuild support: the one sanctioned way to move a high-water mark back."""
        ...

    async def prune_events(self, older_than: datetime) -> int:
        """Rolling mode only: delete events all projectors have applied (D-45)."""
        ...

    async def upsert_record(self, record: MemoryRecord) -> None: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...

    async def find_active_fact(
        self, namespace: str, entity: str, attribute: str
    ) -> MemoryRecord | None:
        """The currently-valid record for one (entity, attribute) key (M4)."""
        ...

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...
