"""Resource memory store (M13.9/D-29): ingest documents through the write door.

Each chunk becomes one ``resource`` record whose provenance names the source
document (``source.doc_path``) and chunk index (``source.message_id``) — the
E1 taint trail from any retrieved chunk back to the file that produced it.
The PII tier set at ingest propagates to every chunk (M7).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import ClassVar

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, PiiTier, SourceInfo
from memspine.memories.base import BaseMemory
from memspine.memories.resource.extraction import chunk_text, extract_text
from memspine.observability.logging import get_logger

__all__ = ["ResourceMemory"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


class ResourceMemory(BaseMemory):
    name: ClassVar[str] = "resource"

    def __init__(self, append_event: AppendEvent) -> None:
        self._append_event = append_event

    async def ingest(
        self,
        namespace: str,
        path: str | Path,
        pii_tier: PiiTier = PiiTier.NONE,
        actor: str = "user",
    ) -> list[MemoryRecord]:
        """Extract → chunk → one WRITE event per chunk. Returns the records."""
        source_path = Path(path)
        text = await asyncio.to_thread(extract_text, source_path)
        chunks = await asyncio.to_thread(chunk_text, text)
        records: list[MemoryRecord] = []
        for index, chunk in enumerate(chunks):
            record = MemoryRecord(
                namespace=namespace,
                memory_type="resource",
                content=chunk,
                pii_tier=pii_tier,
                source=SourceInfo(
                    role=actor,
                    channel="ingest",
                    doc_path=str(source_path),
                    message_id=f"chunk:{index}",
                ),
            )
            await self._append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace=namespace,
                    actor=actor,
                    payload={"record": record.model_dump(mode="json")},
                )
            )
            records.append(record)
        _log.info(
            "memory.write",
            namespace=namespace,
            ingest=str(source_path),
            chunks=len(records),
        )
        return records
