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

    def __init__(self, store: LexicalStore, name: str = "lexical") -> None:
        self._store = store
        #: N58: a non-default analyzer projects under its own name, so its offset
        #: starts at 0 and the index is rebuilt from the log.
        self.name = name

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

    def begin_batch(self) -> None:
        # Stores that can hold their commit until a read or flush() (Tantivy)
        # do so for the batch; any other store commits per apply as before.
        defer = getattr(self._store, "defer_commits", None)
        if defer is not None:
            defer()

    async def flush(self) -> None:
        flush = getattr(self._store, "flush", None)
        if flush is not None:
            await flush()
