"""LanceDB connection client (D-24), ``[lance]``.

Owns the ``lancedb.connect`` handle; the vector service receives this client
injected and never connects itself. Import of the lancedb package is deferred
to ``connect()`` so constructing the client without the extra fails with the
actionable D-10 error, and a core install never imports it at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from memspine.clients.base import Client
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["LanceDBClient"]


class LanceDBClient(Client):
    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._db: Any = None

    @property
    def db(self) -> Any:
        if self._db is None:
            raise StorageError("LanceDBClient is not connected — call connect() first")
        return self._db

    async def connect(self) -> None:
        if self._db is not None:
            return
        try:
            import lancedb
        except ImportError as exc:
            raise MissingServiceError("vector:lancedb", extra="lance") from exc
        self._db = await asyncio.to_thread(lancedb.connect, self._path)

    async def close(self) -> None:
        # lancedb connections hold no server socket; drop the handle so table
        # references are released and a fresh connect() reopens cleanly.
        self._db = None

    async def health(self) -> bool:
        return self._db is not None
