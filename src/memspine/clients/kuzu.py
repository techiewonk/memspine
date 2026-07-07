"""Kuzu connection client (D-24), ``[kuzu]``.

Owns the embedded ``kuzu.Database`` + ``kuzu.Connection``; the graph service
receives this client injected and never connects itself (D-22). Import of the
kuzu package is deferred to ``connect()`` so constructing the client without
the extra fails with the actionable D-10 error, and a core install never
imports it at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from memspine.clients.base import Client
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["KuzuClient"]


class KuzuClient(Client):
    """One embedded kuzu database per path (``":memory:"`` for throwaway)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._db: Any = None
        self._conn: Any = None

    @property
    def connection(self) -> Any:
        if self._conn is None:
            raise StorageError("KuzuClient is not connected — call connect() first")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            import kuzu
        except ImportError as exc:
            raise MissingServiceError("graph:kuzu", extra="kuzu") from exc

        def _open() -> tuple[Any, Any]:
            db = kuzu.Database(self._path)
            return db, kuzu.Connection(db)

        self._db, self._conn = await asyncio.to_thread(_open)

    async def close(self) -> None:
        conn, db = self._conn, self._db
        self._conn = self._db = None

        def _close() -> None:
            if conn is not None:
                conn.close()
            if db is not None:
                db.close()

        await asyncio.to_thread(_close)

    async def health(self) -> bool:
        return self._conn is not None
