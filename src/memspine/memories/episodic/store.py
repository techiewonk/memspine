"""Episodic memory package (M13.2): timeline + session reads.

Writes need no special path — episodic records flow through the ordinary
write door (Engine.write / working-memory page-outs). This package owns the
*reads* that make episodic episodic: chronological timelines and derived
sessions. Consolidation (M2) consumes both from the pipelines.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import ClassVar, Protocol

from memspine.config.constants import SESSION_GAP_MINUTES
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.base import BaseMemory
from memspine.memories.episodic.sessions import Session, detect_sessions
from memspine.memories.episodic.timeops import sort_timeline, within

__all__ = ["EpisodicMemory"]


class EpisodicStore(Protocol):
    """The storage slice episodic reads need (ports & adapters, D-22)."""

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...


class EpisodicMemory(BaseMemory):
    name: ClassVar[str] = "episodic"

    def __init__(self, storage: EpisodicStore) -> None:
        self._storage = storage

    async def _active(self, namespace: str) -> list[MemoryRecord]:
        records = await self._storage.list_records(namespace, "episodic")
        return [record for record in records if record.status is not RecordStatus.DELETED]

    async def timeline(
        self,
        namespace: str,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> list[MemoryRecord]:
        """Chronological (event-time) episodic records, optionally windowed."""
        return sort_timeline(within(await self._active(namespace), start, end))

    async def sessions(
        self, namespace: str, gap_minutes: int = SESSION_GAP_MINUTES
    ) -> list[Session]:
        """Derived session boundaries over the namespace's episodic timeline."""
        return detect_sessions(await self._active(namespace), timedelta(minutes=gap_minutes))
