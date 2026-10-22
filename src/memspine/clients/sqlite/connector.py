"""SQLite connector: async SQLAlchemy engine over aiosqlite (D-24/D-36/D-44, RM-113).

Owns the engine and the WAL/pragma setup. The storage adapter receives this connector
injected; it never creates an engine itself. Sync engines for the same database (outbox,
leases) and the connection Alembic migrates over are lent by :meth:`SQLiteClient.sync_engine`
and :meth:`SQLiteClient.migration_connection` (``clients/sqlite/sync.py``).

Concurrency model: file-backed databases use SQLAlchemy's default async pool
(one connection per concurrent transaction, WAL handles multi-reader).
``:memory:`` databases use a *named shared-cache* in-memory URI so the pool's
connections all see the same database â€” a plain ``:memory:`` would give every
pooled connection its own empty database, and a single shared static
connection races under concurrent asyncio transactions. An anchor connection
held for the client's lifetime keeps the shared in-memory database alive even
when the pool is momentarily empty.

Encryption at rest (#52, ADR-035, opt-in): ``cipher_key_env`` names the
environment variable that holds a SQLCipher passphrase. Every pooled connection
is then opened through the ``sqlcipher3`` (or ``pysqlcipher3``) driver, keyed with
``PRAGMA key`` before any other statement, and checked by one read so a wrong key
fails at connect time. The key is read from that variable only, once per
``open()``; it is never logged, never stored on a public attribute and never
part of ``repr()`` or an error message.
"""

from __future__ import annotations

import asyncio
import importlib
import itertools
import math
import os
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from sqlalchemy import event as sa_event
from sqlalchemy import text
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine
from sqlalchemy.pool import AsyncAdaptedQueuePool

from memspine.clients._common.deadlines import probe
from memspine.clients._common.secrets import Secret, auth_digest, env_or_empty
from memspine.clients.base import ConnectorIdentity, Locality, Scope
from memspine.exceptions import ConfigError, MissingServiceError, StorageError

if TYPE_CHECKING:
    from sqlalchemy.engine import Connection, Engine

__all__ = [
    "SQLITE_BUSY_TIMEOUT_MS",
    "SQLiteClient",
    "SQLiteSpec",
    "load_sqlcipher_driver",
    "path_identity",
]

_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA foreign_keys=ON",
    # #43: deleted and overwritten rows are zeroed on disk, not left in free
    # pages, so a hard forget leaves no recoverable bytes in the database file.
    "PRAGMA secure_delete=ON",
)

MEMORY = ":memory:"

#: SQLite busy timeout (ms) on every file connection: how long a statement waits for another
#: writer's lock before "database is locked". #88: the first connection of a client retries
#: the WAL switch for the same span, since that switch does not consult the busy handler.
SQLITE_BUSY_TIMEOUT_MS = 5000


def path_identity(path: str | os.PathLike[str]) -> str:
    """Canonical identity of a local file path (absolute, case-folded where the platform
    folds case), so two spellings of one database share one connector."""
    return os.path.normcase(str(Path(path).expanduser().resolve(strict=False)))


@dataclass(frozen=True, slots=True)
class SQLiteSpec:
    """Transport spec of one SQLite database (``":memory:"`` for a private throwaway one).

    ``pool_options`` (``runtime.shared_resources.sql``): SQLAlchemy queue-pool keyword
    arguments for a file database; ignored by ``:memory:`` and SQLCipher (fixed pools)."""

    path: str | os.PathLike[str] = MEMORY
    echo: bool = False
    cipher_key_env: str | None = None
    busy_timeout_s: float = SQLITE_BUSY_TIMEOUT_MS / 1000
    pool_options: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        busy = self.busy_timeout_s
        if isinstance(busy, bool) or not math.isfinite(busy) or busy <= 0:
            raise ValueError("busy_timeout_s must be finite and positive")
        object.__setattr__(self, "path", str(self.path))
        object.__setattr__(self, "pool_options", dict(self.pool_options))

    @property
    def is_memory(self) -> bool:
        return str(self.path) == MEMORY


def _is_locked(exc: BaseException) -> bool:
    return "database is locked" in str(exc) or "database is busy" in str(exc)


async def _first_connection(engine: AsyncEngine, busy_timeout_ms: int) -> None:
    """Open (and return to the pool) one connection, retrying while another
    process holds the lock the WAL switch needs (the switch does not wait on its
    own, see :func:`_ensure_wal`), for up to the busy timeout."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + busy_timeout_ms / 1000
    delay = 0.01
    while True:
        try:
            async with engine.connect():
                return
        except OperationalError as exc:
            if not _is_locked(exc) or loop.time() >= deadline:
                raise
        await asyncio.sleep(delay)
        delay = min(delay * 2, 0.25)


def _ensure_wal(cursor: Any) -> None:
    """Switch the database to WAL unless it already is.

    #88 (flaky "database is locked" with two engines on one file): the Alembic
    migration creates a database file in rollback mode, and the switch to WAL was
    left to the first pooled connection, which ran ``PRAGMA journal_mode=WAL``
    unconditionally. With two engines on a fresh file, both first connections
    asked to switch at once; the switch needs an exclusive lock and does not
    consult the busy handler, so one failed at once (about 1 ms, not after the
    5 s timeout). Now :meth:`SQLiteClient.open` switches the file before the
    migration (retrying, see :func:`_first_connection`), and later connections
    only check: one read of the schema (a shared lock, which does wait) loads the
    header, after which the pager reports ``wal`` and nothing is switched.
    """
    cursor.execute("PRAGMA schema_version")
    cursor.fetchone()
    cursor.execute("PRAGMA journal_mode")
    row = cursor.fetchone()
    if row is None or str(row[0]).lower() != "wal":
        cursor.execute("PRAGMA journal_mode=WAL")


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


def _pragma_key(key: Secret) -> str:
    """``PRAGMA key`` with the passphrase as a single-quoted SQL literal."""
    return "PRAGMA key = '" + key.reveal().replace("'", "''") + "'"


class SQLiteClient:
    """One async engine per database file (or ``:memory:``); a :class:`Connector`."""

    kind: ClassVar[str] = "sql.sqlite"
    scope: ClassVar[Scope] = "loop"

    def __init__(self, spec: SQLiteSpec) -> None:
        self._spec = spec
        self._busy_timeout_ms = max(1, math.ceil(spec.busy_timeout_s * 1000))
        self._engine: AsyncEngine | None = None
        self._anchor: AsyncConnection | None = None
        #: #52: opens one keyed SQLCipher DB-API connection (set by ``open()``).
        self._cipher_connector: Callable[[], Any] | None = None

    # -- connector protocol ----------------------------------------------------------------
    @classmethod
    def identity(cls, spec: SQLiteSpec) -> ConnectorIdentity:
        """A file database by canonical path (and key digest under SQLCipher). A ``:memory:``
        database is private to its connector: its identity never equals another's."""
        if spec.is_memory:
            return ConnectorIdentity(cls.kind, {"path": MEMORY, "private": uuid.uuid4().hex})
        key_env = spec.cipher_key_env
        return ConnectorIdentity(
            cls.kind,
            {"path": path_identity(spec.path)},
            auth_digest(env_or_empty(key_env)) if key_env else None,
            {
                "busy_timeout_s": spec.busy_timeout_s,
                "encryption": "sqlcipher" if key_env else "none",
                # SQLCipher opens through one keyed connection: pool options do not apply.
                "pool": {} if key_env else dict(spec.pool_options),
            },
            {"kind": "sqlite", "path": path_identity(spec.path)},
        )

    @classmethod
    def locality(cls, spec: SQLiteSpec) -> Locality:
        return "process" if spec.is_memory else "host"

    @property
    def spec(self) -> SQLiteSpec:
        return self._spec

    @property
    def handle(self) -> AsyncEngine:
        return self.engine

    def __repr__(self) -> str:
        cipher = "sqlcipher" if self._spec.cipher_key_env else "none"
        return f"SQLiteClient(path={self.path!r}, encryption={cipher!r})"

    @property
    def encrypted(self) -> bool:
        """Whether connections are opened through SQLCipher (#52)."""
        return self._spec.cipher_key_env is not None

    @property
    def sync_creator(self) -> Callable[[], Any] | None:
        """#52: a zero-argument callable returning a keyed (sync) SQLCipher DB-API
        connection; None for a plain database. Available after ``open()``."""
        return self._cipher_connector

    @property
    def path(self) -> str:
        return str(self._spec.path)

    @property
    def busy_timeout_s(self) -> float:
        return self._busy_timeout_ms / 1000

    @property
    def is_memory(self) -> bool:
        return self._spec.is_memory

    @property
    def engine(self) -> AsyncEngine:
        if self._engine is None:
            raise StorageError("SQLiteClient is not open â€” call open() first")
        return self._engine

    async def open(self) -> None:
        if self._engine is not None:
            return
        spec = self._spec
        if spec.cipher_key_env is not None:
            engine = self._cipher_engine(spec.cipher_key_env)
        elif self.is_memory:
            # Unique per client: two ':memory:' clients must not share state.
            # poolclass is forced explicitly â€” SQLAlchemy's aiosqlite dialect
            # sees 'mode=memory' and would silently select StaticPool (one
            # shared connection), reintroducing the exact concurrent-
            # transaction interleaving this shared-cache URI exists to fix.
            name = f"memspine_mem_{next(_memory_db_counter)}"
            url = f"sqlite+aiosqlite:///file:{name}?mode=memory&cache=shared&uri=true"
            engine = create_async_engine(url, echo=spec.echo, poolclass=AsyncAdaptedQueuePool)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            url = f"sqlite+aiosqlite:///{self.path}"
            engine = create_async_engine(url, echo=spec.echo, **dict(spec.pool_options))

        @sa_event.listens_for(engine.sync_engine, "connect")
        def _set_pragmas(dbapi_conn: Any, _record: Any) -> None:
            cursor = dbapi_conn.cursor()
            cursor.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            if not self.is_memory:
                _ensure_wal(cursor)
            for pragma in _PRAGMAS:
                cursor.execute(pragma)
            cursor.close()

        if not self.is_memory:
            # #88: open one connection now, so the file is in WAL before anything
            # else touches it (the Alembic migration creates it in rollback mode
            # otherwise, and the first pooled connections of two engines then race
            # to switch it). #52: a wrong key or a plain-SQLite file also fails here,
            # at start, instead of at the first write.
            try:
                await _first_connection(engine, self._busy_timeout_ms)
            except BaseException:
                await engine.dispose()
                raise
        else:
            # Keep the shared in-memory database alive across pool churn. The
            # engine is only published once the anchor holds, so a failed
            # anchor cannot leave a half-open client that no-ops retries.
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
        key = Secret(raw)
        del raw
        path = self.path
        Path(path).parent.mkdir(parents=True, exist_ok=True)

        def connector() -> Any:
            conn = driver.connect(path, check_same_thread=False)
            try:
                conn.execute(_pragma_key(key))  # must precede every other statement
                conn.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
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

        return create_async_engine(
            "sqlite+aiosqlite://", echo=self._spec.echo, async_creator=creator
        )

    async def health(self, deadline_s: float) -> bool:
        """``SELECT 1`` over a pooled connection."""
        engine = self._engine
        if engine is None:
            return False

        async def check() -> bool:
            async with engine.connect() as conn:
                return bool((await conn.scalar(text("SELECT 1"))) == 1)

        return await probe(deadline_s, check)

    async def checkpoint(self) -> None:
        """#43: fold the WAL into the database and truncate it, so the pre-erasure
        page images a hard forget superseded do not survive in the ``-wal`` file.
        A no-op for ``:memory:`` (no WAL) and before ``open()``."""
        if self._engine is None or self.is_memory:
            return
        async with self._engine.connect() as conn:
            await conn.exec_driver_sql("PRAGMA wal_checkpoint(TRUNCATE)")

    async def close(self) -> None:
        if self._anchor is not None:
            anchor, self._anchor = self._anchor, None
            await anchor.close()
        if self._engine is not None:
            engine, self._engine = self._engine, None
            await engine.dispose()

    # -- lent sync access (clients/sqlite/sync.py) -------------------------------------------
    def sync_engine(self, pool_options: Mapping[str, Any] | None = None) -> Engine:
        """A new sync engine over this file database (keyed under SQLCipher; available after
        ``open()``). The caller owns it and disposes it. ``:memory:`` has no second engine
        that could reach it; use :func:`~memspine.clients.sqlite.sync.memory_sync_engine`."""
        from memspine.clients.sqlite.sync import sqlite_sync_engine

        if self.is_memory:
            raise StorageError("a ':memory:' database cannot be reached by a second engine")
        if self.encrypted and self._cipher_connector is None:
            raise StorageError("SQLiteClient is not open â€” call open() first")
        return sqlite_sync_engine(
            self.path,
            self._cipher_connector,
            busy_timeout_s=self.busy_timeout_s,
            pool_options=pool_options,
        )

    @contextmanager
    def migration_connection(self) -> Iterator[Connection]:
        """One sync connection (keyed under SQLCipher) for an Alembic run; closed on exit.

        Refuses ``:memory:``: a migration connection cannot reach an in-memory database."""
        from memspine.clients.sqlite.sync import migration_connection

        if self.is_memory:
            raise StorageError(
                "cannot run Alembic against ':memory:' â€” a migration connection cannot "
                "reach an in-memory database; use metadata.create_all instead"
            )
        with migration_connection(self.path, self._cipher_connector) as connection:
            yield connection
