"""LanceDB connection client (D-24, core dep — ADR-021).

Owns the ``lancedb.connect`` handle; the vector service receives this client
injected and never connects itself.
"""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from typing import Any

from memspine.clients.base import Client
from memspine.config import constants
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["LanceDBClient"]


class LanceDBClient(Client):
    """One connection per database path per process (RM-77: shared by every Engine on the path).

    LanceDB table handles stay on the version they last read or wrote, so a handle opened by
    one store does not see rows another store committed through its own handle. Each store
    reports its commits here (:meth:`note_write`); a store whose handle is behind the table's
    write epoch checks out the latest version before it reads (one integer compare otherwise).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._db: Any = None
        self._epochs: dict[str, int] = {}
        self._epoch_lock = threading.Lock()

    def write_epoch(self, table: str) -> int:
        """How many commits the stores of this client made to ``table``."""
        return self._epochs.get(table, 0)

    def note_write(self, table: str) -> int:
        """Record one commit to ``table``; returns the new epoch."""
        with self._epoch_lock:
            epoch = self._epochs[table] = self._epochs.get(table, 0) + 1
            return epoch

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
            raise MissingServiceError("vector:lancedb") from exc
        if self._path.startswith("memory://"):
            # ADR-053: a small metadata cache, so dropped table versions are
            # released instead of pinned by cache entries.
            session = lancedb.Session(
                metadata_cache_size_bytes=constants.LANCE_MEMORY_METADATA_CACHE_BYTES
            )
            self._db = await asyncio.to_thread(lancedb.connect, self._path, session=session)
        else:
            self._db = await asyncio.to_thread(lancedb.connect, self._path)

    async def close(self) -> None:
        # lancedb connections hold no server socket; drop the handle so table
        # references are released and a fresh connect() reopens cleanly.
        self._db = None

    async def health(self) -> bool:
        return self._db is not None
