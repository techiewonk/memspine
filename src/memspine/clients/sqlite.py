"""SQLite connection client: async SQLAlchemy engine over aiosqlite (D-24/D-36/D-44).

Owns the engine and the WAL/pragma setup. The storage service receives this
client injected; it never creates an engine itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from memspine.clients.base import Client
from memspine.exceptions import StorageError

__all__ = ["SQLiteClient"]

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
)


class SQLiteClient(Client):
    """One async engine per database file (or ``:memory:``)."""

    def __init__(self, path: str | Path = ":memory:", echo: bool = False) -> None:
        self._path = str(path)
        self._echo = echo
        self._engine: AsyncEngine | None = None

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
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        url = (
            f"sqlite+aiosqlite:///{self._path}"
            if self._path != ":memory:"
            else ("sqlite+aiosqlite://")
        )
        engine = create_async_engine(url, echo=self._echo)

        @sa_event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            for pragma in _PRAGMAS:
                cursor.execute(pragma)
            cursor.close()

        self._engine = engine

    async def close(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def health(self) -> bool:
        return self._engine is not None
