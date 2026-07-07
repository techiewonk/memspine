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
from typing import ClassVar, Protocol

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, PiiTier, SourceInfo
from memspine.memories.base import BaseMemory
from memspine.memories.resource.extraction import chunk_text, extract_text
from memspine.observability.logging import get_logger

__all__ = ["ResourceMemory"]

_log = get_logger(__name__)

AppendEvent = Callable[[MemoryEvent], Awaitable[None]]


class ResourceStore(Protocol):
    """Read slice for ingest idempotency (ports & adapters, D-22)."""

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]: ...


class ResourceMemory(BaseMemory):
    name: ClassVar[str] = "resource"

    def __init__(self, append_event: AppendEvent, storage: ResourceStore | None = None) -> None:
        self._append_event = append_event
        self._storage = storage

    async def ingest(
        self,
        namespace: str,
        path: str | Path,
        pii_tier: PiiTier = PiiTier.NONE,
        actor: str = "user",
    ) -> list[MemoryRecord]:
        """Extract → chunk → one WRITE event per chunk. Returns the NEW records.

        Idempotent per (doc_path, chunk content): re-ingesting after a partial
        failure skips chunks that already landed instead of duplicating them.
        A mid-loop failure logs how far it got before re-raising — a torn
        ingest must be discoverable from the logs alone.
        """
        source_path = Path(path)
        text = await asyncio.to_thread(extract_text, source_path)
        chunks = await asyncio.to_thread(chunk_text, text)
        if not chunks:
            _log.warning(
                "resource.ingest_empty",
                namespace=namespace,
                ingest=str(source_path),
                detail="document produced zero usable chunks — nothing written",
            )
            return []
        already = set()
        if self._storage is not None:
            already = {
                (record.source.doc_path, record.content_fingerprint)
                for record in await self._storage.list_records(namespace, "resource")
            }
        records: list[MemoryRecord] = []
        skipped = 0
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
            if (str(source_path), record.content_fingerprint) in already:
                skipped += 1
                continue  # retry after partial failure: chunk already landed
            try:
                await self._append_event(
                    MemoryEvent(
                        kind=EventKind.WRITE,
                        namespace=namespace,
                        actor=actor,
                        payload={"record": record.model_dump(mode="json")},
                    )
                )
            except Exception:
                _log.warning(
                    "resource.ingest_partial",
                    namespace=namespace,
                    ingest=str(source_path),
                    chunks_written=len(records),
                    chunks_total=len(chunks),
                    failed_at=index,
                )
                raise
            records.append(record)
        _log.info(
            "memory.write",
            namespace=namespace,
            ingest=str(source_path),
            chunks=len(records),
            skipped_existing=skipped,
        )
        return records
