"""Vector projector: embeds WRITE events into the vector store (D0.1).

A projection like any other — rebuild re-embeds the full log (the E3 embedding
cache makes that cheap when content is unchanged).
"""

from __future__ import annotations

from memspine.services.embedding.base import EmbeddingService
from memspine.services.vector.base import VectorStore
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.projections.projector import Projector
from memspine.store.records import MemoryRecord

__all__ = ["VectorProjector"]


class VectorProjector(Projector):
    name = "vectors"

    def __init__(self, store: VectorStore, embedder: EmbeddingService) -> None:
        self._store = store
        self._embedder = embedder

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            record = MemoryRecord.model_validate(event.payload["record"])
            # R14-INDEXVIEW: an index text (write.index_view) is embedded; absent = the content
            text = event.payload.get("index_text")
            body = text if isinstance(text, str) and text else record.content
            [vector] = await self._embedder.embed([body])
            await self._store.upsert(
                record.record_id, record.namespace, self._embedder.embedder_id, vector
            )
        elif event.kind is EventKind.FORGET:
            # A forgotten memory must stop being retrievable (delete is
            # idempotent, so replay is safe).
            await self._store.delete(str(event.payload["record_id"]))

    async def reset(self) -> None:
        await self._store.delete_all()
