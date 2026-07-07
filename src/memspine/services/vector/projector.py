"""Vector projector: embeds WRITE events into the vector store (D0.1).

A projection like any other — rebuild re-embeds the full log (the E3 embedding
cache makes that cheap when content is unchanged).
"""

from __future__ import annotations

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord
from memspine.services.embedding.base import EmbeddingService
from memspine.services.vector.base import VectorStore

__all__ = ["VectorProjector"]


class VectorProjector(Projector):
    name = "vectors"

    def __init__(self, store: VectorStore, embedder: EmbeddingService) -> None:
        self._store = store
        self._embedder = embedder

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is not EventKind.WRITE:
            return
        record = MemoryRecord.model_validate(event.payload["record"])
        [vector] = await self._embedder.embed([record.content])
        await self._store.upsert(
            record.record_id, record.namespace, self._embedder.embedder_id, vector
        )

    async def reset(self) -> None:
        await self._store.delete_all()
