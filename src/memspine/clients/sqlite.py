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

Encryption at rest (#52, ADR-035, opt-in): ``cipher_key_env`` names the
environment variable that holds a SQLCipher passphrase. Every pooled connection
is then opened through the ``sqlcipher3`` (or ``pysqlcipher3``) driver, keyed with
``PRAGMA key`` before any other statement, and checked by one read so a wrong key
fails at connect time. The key is read from that variable only, once per
``connect()``; it is never logged, never stored on a public attribute and never
part of ``repr()`` or an error message.
"""

from __future__ import annotations

import importlib
import itertools
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import event as sa_event
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from memspine.clients.base import Client
from memspine.exceptions import ConfigError, MissingServiceError, StorageError

__all__ = ["SQLiteClient"]

_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    "PRAGMA busy_timeout=5000",
    # #43: deleted and overwritten rows are zeroed on disk, not left in free
    # pages, so a hard forget leaves no recoverable bytes in the database file.
    "PRAGMA secure_delete=ON",
)

_memory_db_counter = itertools.count(1)

#: SQLCipher DB-API modules tried in order (#52); the ``[encrypt]`` extra installs the first.
_SQLCIPHER_DRIVERS = ("sqlcipher3.dbapi2", "pysqlcipher3.dbapi2")
#: rows aiosqlite fetches per chunk (its own ``connect()`` default).
_AIOSQLITE_CHUNK = 64


def load_sqlcipher_driver() -> Any:
    """The installed SQLCipher DB-API module, or ``MissingServiceError`` naming ``[encrypt]``."""
    for name in _SQLCIPHER_DRIVERS:
        try:
            return importlib.import_module(name)
        except ImportError:
            continue
    raise MissingServiceError("storage.encryption.sqlcipher", "encrypt")


class _Secret:
    """Holds a key so that no ``repr``/``str``/log rendering can show it."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__


def _pragma_key(key: _Secret) -> str:
    """``PRAGMA key`` with the passphrase as a single-quoted SQL literal."""
    return "PRAGMA key = '" + key.reveal().replace("'", "''") + "'"


class SQLiteClient(Client):
    """One async engine per database file (or ``:memory:``)."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        echo: bool = False,
        *,
        cipher_key_env: str | None = None,
    ) -> None:
        self._path = str(path)
        self._echo = echo
        self._cipher_key_env = cipher_key_env
        self._engine: AsyncEngine | None = None
        self._anchor: AsyncConnection | None = None
        #: #52: opens one keyed SQLCipher DB-API connection (set by ``connect()``).
        self._cipher_connector: Callable[[], Any] | None = None

    def __repr__(self) -> str:
        cipher = "sqlcipher" if self._cipher_key_env else "none"
        return f"SQLiteClient(path={self._path!r}, encryption={cipher!r})"

    @property
    def encrypted(self) -> bool:
        """Whether connections are opened through SQLCipher (#52)."""
        return self._cipher_key_env is not None

    @property
    def sync_creator(self) -> Callable[[], Any] | None:
        """#52: a zero-argument callable returning a keyed (sync) SQLCipher DB-API
        connection, for the Alembic migration run; None for a plain database.
        Available after ``connect()``."""
        return self._cipher_connector

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
        if self._cipher_key_env is not None:
            engine = self._cipher_engine(self._cipher_key_env)
        elif self.is_memory:
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

        if self.encrypted:
            # #52: open one connection now, so a wrong key or a plain-SQLite file
            # fails at start instead of at the first write.
            try:
                async with engine.connect():
                    pass
            except BaseException:
                await engine.dispose()
                raise
        elif self.is_memory:
            # Keep the shared in-memory database alive across pool churn. The
            # engine is only published once the anchor holds, so a failed
            # anchor cannot leave a half-connected client that no-ops retries.
            try:
                self._anchor = await engine.connect()
            except BaseException:
                await engine.dispose()
                raise
        self._engine = engine

    def _cipher_engine(self, key_env: str) -> AsyncEngine:
        """#52: an async engine whose every connection is a keyed SQLCipher connection."""
        if self.is_memory:
            raise ConfigError(
                "storage.encryption.mode=sqlcipher needs a file database, not ':memory:'"
            )
        driver = load_sqlcipher_driver()
        raw = os.environ.get(key_env)
        if not raw:
            raise ConfigError(
                f"storage.encryption.key_env names {key_env!r}, which is not set or empty"
            )
        key = _Secret(raw)
        del raw
        path = self._path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        def connector() -> Any:
            conn = driver.connect(path, check_same_thread=False)
            try:
                conn.execute(_pragma_key(key))  # must precede every other statement
                conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            except Exception:
                conn.close()
                # The driver's message never carries the key, but say nothing of it.
                raise StorageError(
                    f"cannot open encrypted database {path!r}: wrong key, or not a "
                    "SQLCipher database"
                ) from None
            return conn

        self._cipher_connector = connector

        async def creator() -> Any:
            import aiosqlite

            return await aiosqlite.Connection(connector, _AIOSQLITE_CHUNK)

        return create_async_engine("sqlite+aiosqlite://", echo=self._echo, async_creator=creator)

    async def checkpoint(self) -> None:
        """#43: fold the WAL into the database and truncate it, so the pre-erasure
        page images a hard forget superseded do not survive in the ``-wal`` file.
        A no-op for ``:memory:`` (no WAL) and before ``connect()``."""
        if self._engine is None or self.is_memory:
            return
        async with self._engine.connect() as conn:
            await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    async def close(self) -> None:
        if self._anchor is not None:
            await self._anchor.close()
            self._anchor = None
        if self._engine is not None:
            await self._engine.dispose()
            self._engine = None

    async def health(self) -> bool:
        return self._engine is not None
