"""LadybugDB connection client (D-26/D-24), ``[graph]``.

LadybugDB (PyPI: ``ladybug``, import ``ladybug``, pinned ``>=0.21,<0.22``) is
the maintained, MIT-licensed fork of Kùzu; Kùzu itself was archived upstream on
2025-10-10. The fork kept Kùzu's embedded-Cypher Python API (``Database``/
``Connection``, synchronous client, the same DDL dialect), so this client
mirrors :class:`~memspine.clients.kuzu.KuzuClient` (deprecated, ADR-034).

Owns the embedded ``ladybug.Database`` + ``ladybug.Connection``; the graph
service receives this client injected and never connects itself (D-22).
Import of the ``ladybug`` package is deferred to ``connect()`` so constructing
the client without the extra fails with the actionable D-10 error, and a core
install never imports it at all.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from memspine.clients.base import Client
from memspine.exceptions import MissingServiceError, StorageError

__all__ = ["LadybugClient"]


class LadybugClient(Client):
    """One embedded ladybug database per path (``":memory:"`` for throwaway)."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._path = str(path)
        self._db: Any = None
        self._conn: Any = None

    @property
    def connection(self) -> Any:
        if self._conn is None:
            raise StorageError("LadybugClient is not connected — call connect() first")
        return self._conn

    async def connect(self) -> None:
        if self._conn is not None:
            return
        try:
            import ladybug
        except ImportError as exc:
            raise MissingServiceError("graph:ladybug", extra="graph") from exc

        def _open() -> tuple[Any, Any]:
            db = ladybug.Database(self._path)
            return db, ladybug.Connection(db)

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
