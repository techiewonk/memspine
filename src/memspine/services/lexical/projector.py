"""Lexical projector: indexes WRITE content into the lexical store (D0.1).

A projection like any other (mirrors :class:`VectorProjector`): WRITE indexes
the record's content, FORGET drops it. Registered in ``engine._projectors``
ONLY when hybrid retrieval is enabled, so rebuild() replays it and the
``simple`` profile never builds a lexical index (D0.1/ADR-015).
"""

from __future__ import annotations

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord
from memspine.services.lexical.base import LexicalStore

__all__ = ["LexicalProjector"]


class LexicalProjector(Projector):
    name = "lexical"

    def __init__(self, store: LexicalStore) -> None:
        self._store = store

    async def apply(self, event: MemoryEvent) -> None:
        if event.kind is EventKind.WRITE:
            record = MemoryRecord.model_validate(event.payload["record"])
            await self._store.index(record)
        elif event.kind is EventKind.FORGET:
            # A forgotten memory must stop being retrievable via the lexical leg
            # too (delete is idempotent, so replay is safe).
            await self._store.delete(str(event.payload["record_id"]))

    async def reset(self) -> None:
        await self._store.clear()
