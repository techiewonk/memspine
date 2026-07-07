"""Reflective memory store (M13.7): insights derived from episodic experience.

A reflection is an ordinary record (``memory_type="reflective"``) whose WRITE
event names its source records (``reflection.member_record_ids``) — the same
derivation-provenance pattern consolidation uses (P3.1), so ``audit taint``
(E1) propagates through reflections with no special casing. Depth rides
``reflection_depth`` and is capped by ``guards`` (0 raw → 1 reflection →
2 meta, terminal).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import ClassVar, Protocol

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.exceptions import ConflictError
from memspine.memories.base import BaseMemory
from memspine.memories.reflective.guards import reflection_depth_for
from memspine.observability.logging import get_logger

__all__ = ["ReflectiveMemory"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]
#: Firewall gate the engine injects (E1) — same seam as resource ingest;
#: async so the full anomaly context (vector neighbours) participates.
AssessRecord = Callable[[MemoryRecord], Awaitable[MemoryRecord]]


class ReflectiveStore(Protocol):
    """The storage slice reflective needs (ports & adapters, D-22)."""

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...

    async def get_record(self, record_id: str) -> MemoryRecord | None: ...


class ReflectiveMemory(BaseMemory):
    name: ClassVar[str] = "reflective"
    needs: ClassVar[tuple[str, ...]] = ("episodic",)

    def __init__(
        self,
        storage: ReflectiveStore,
        append_event: AppendEvent,
        assess: AssessRecord | None = None,
    ) -> None:
        self._storage = storage
        self._append_event = append_event
        self._assess = assess

    async def reflect(
        self,
        namespace: str,
        content: str,
        source_record_ids: list[str],
        source: SourceInfo | None = None,
    ) -> MemoryRecord:
        """Write one reflection derived from ``source_record_ids``.

        The guards run against the *fetched* parents (never caller-supplied
        depths), so the cap and the quarantine-laundering refusal cannot be
        bypassed by lying about a parent.
        """
        parents: list[MemoryRecord] = []
        for record_id in source_record_ids:
            parent = await self._storage.get_record(record_id)
            if parent is None:
                raise ConflictError(f"reflection source {record_id!r} does not exist")
            parents.append(parent)
        depth = reflection_depth_for(parents)
        record = MemoryRecord(
            namespace=namespace,
            memory_type="reflective",
            content=content,
            reflection_depth=depth,
            source=source or SourceInfo(role="system", channel="reflection"),
        )
        if self._assess is not None:
            record = await self._assess(record)  # E1: reflections pass the gate too
        await self._append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=namespace,
                actor=record.source.role,
                payload={
                    "record": record.model_dump(mode="json"),
                    # Derivation provenance (E1/P3.1): taint flows through here.
                    "reflection": {"member_record_ids": list(source_record_ids)},
                },
            )
        )
        _log.info(
            "memory.write",
            namespace=namespace,
            record_id=record.record_id,
            reflection_depth=depth,
            sources=len(parents),
        )
        return record

    async def reflections(self, namespace: str, *, depth: int | None = None) -> list[MemoryRecord]:
        """Stored reflections, optionally filtered to one depth level."""
        records = await self._storage.list_records(namespace, "reflective")
        if depth is not None:
            records = [record for record in records if record.reflection_depth == depth]
        return records
