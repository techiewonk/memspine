"""SQLite connection client: async SQLAlchemy engine over aiosqlite (D-24/D-36/D-44).

Owns the engine and the WAL/pragma setup. The storage service receives this
client injected; it never creates an engine itself.

Concurrency model: file-backed databases use SQLAlchemy's default async pool
(one connection per concurrent transaction, WAL handles multi-reader).
``:memory:`` databases use a *named shared-cache* in-memory URI so the pool's
connections all see the same database — a plain ``:memory:`` would give every
pooled connection its own empty database, and a single shared static
connection races under concurrent asyncio transactions. An anchor connection
held for the client's lifetime keeps the shared in-memory database alive even
when the pool is momentarily empty.
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from memspine.clients.base import Client
from memspine.exceptions import StorageError

__all__ = ["SQLiteClient"]

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)

_memory_db_counter = itertools.count(1)


class SQLiteClient(Client):
    """One async engine per database file (or ``:memory:``)."""

    def __init__(self, path: str | Path = ":memory:", echo: bool = False) -> None:
        self._path = str(path)
        self._echo = echo
        self._engine: AsyncEngine | None = None
        self._anchor: AsyncConnection | None = None

    @property
    def path(self) -> str:
        return self._path

    @property
    def is_memory(self) -> bool:
        return self._path == ":memory:"

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise StorageError("SQLiteClient is not connected — call connect() first")
        return self._engine

    async def connect(self) -> None:
        if self._engine is not None:
            return
        if self.is_memory:
            # Unique per client: two ':memory:' clients must not share state.
            # poolclass is forced explicitly — SQLAlchemy's aiosqlite dialect
            # sees 'mode=memory' and would silently select StaticPool (one
            # shared connection), reintroducing the exact concurrent-
            # transaction interleaving this shared-cache URI exists to fix.
            name = f"memspine_mem_{next(_memory_db_counter)}"
            url = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
            engine = create_async_engine(url, echo=self._echo, poolclass=AsyncAdaptedQueuePool)
        else:
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{self._path}"
            engine = create_async_engine(url, echo=self._echo)

        @sa_event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            for pragma in _PRAGMAS:
                cursor.execute(pragma)
            cursor.close()

        if self.is_memory:
            # Keep the shared in-memory database alive across pool churn. The
            # engine is only published once the anchor holds, so a failed
            # anchor cannot leave a half-connected client that no-ops retries.
            try:
                self._anchor = await engine.connect()
            except BaseException:
                await engine.dispose()
                raise
        self._engine = engine

    async def close(self) -> None:
        if self._anchor is not None:
            await self._anchor.close()
            self._anchor = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def health(self) -> bool:
        return self._engine is not None
