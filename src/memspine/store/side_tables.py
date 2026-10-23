"""RM-111 WP5 (ADR-081 decision 7): sleep leases, the cost ledger and dead letters as tables of the
authoritative SQL database.

Before WP5 these lived in raw-``sqlite3`` side files next to the store (``<base>.sleep``,
``<base>.cost``, ``<base>.deadletters.sqlite``), which bypassed SQLCipher and were not shared by
processes on one Postgres database. Now they are tables of the storage database itself: encrypted
with it on SQLite (the sync engine opens keyed connections), shared on Postgres.

* :data:`side_metadata` holds the tables. Alembic creates them (revision ``rm111_side_tables`` /
  ``pg_rm111_side_tables``); :func:`ensure_side_tables` creates any that are missing for a database
  brought up by ``create_all`` (the embedded Postgres path), under the store's advisory lock.
* :func:`write_tx` is one write transaction: ``BEGIN IMMEDIATE`` on SQLite (the cross-process writer
  lock), an advisory transaction lock on Postgres when asked for.
* :class:`BackgroundWriter` runs writes on one worker thread, in order. The event loop must never
  wait on SQLite's write lock: the async storage layer can hold it across an await, and only the loop
  can release it (the same rule as ``runtime.runner.OutboxRunner._io``).
* :func:`import_side_files` is the one-time import of an existing side file: its rows are copied
  idempotently (a content digest is recorded in ``side_file_imports`` in the same transaction) and
  the file is renamed to ``*.imported``. The caller lends the file opener (RM-113: the store never
  opens a connection; ``memspine.providers.connectors.legacy_sqlite_file``).

``:memory:`` stores keep their private in-memory tables (the stores accept a path there).
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog
from sqlalchemy import (
    BigInteger,
    Column,
    Float,
    Index,
    Integer,
    MetaData,
    Table,
    Text,
    create_engine,
    text,
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

#: Opens one legacy side file for reading (lent by the composition layer).
FileOpener = Callable[[Path], AbstractContextManager[sqlite3.Connection]]

__all__ = [
    "SIDE_FILES",
    "SIDE_TABLES",
    "BackgroundWriter",
    "FileOpener",
    "SideImport",
    "ensure_side_tables",
    "import_side_files",
    "is_postgres",
    "local_engine",
    "side_metadata",
    "write_tx",
]

_log = structlog.get_logger(__name__)

#: Advisory-lock key of every side-table transaction that needs one (the lease store's key).
ADVISORY_KEY = 0x4D535053

#: The ``id`` of an append-only table: BIGSERIAL on Postgres, the rowid alias on SQLite.
_ID = BigInteger().with_variant(Integer(), "sqlite")

side_metadata = MetaData()

sleep_lease = Table(
    "sleep_lease",
    side_metadata,
    Column("ns", Text, primary_key=True),
    Column("owner", Text, nullable=False),
    Column("epoch", Integer, nullable=False),
    Column("job_id", Text, nullable=False),
    Column("lease_until", Float, nullable=False),
    Column("heartbeat_at", Float, nullable=False),
)
sleep_cycle = Table(
    "sleep_cycle",
    side_metadata,
    Column("ns", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("cycle", Integer, nullable=False),
    Column("status", Text, nullable=False),
)
sleep_watermark = Table(
    "sleep_watermark",
    side_metadata,
    Column("ns", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("cycle", Integer, primary_key=True),
    Column("stage", Text, primary_key=True),
    Column("seq", Integer, nullable=False),
    Column("done_at", Float, nullable=False),
)
sleep_state = Table(
    "sleep_state",
    side_metadata,
    Column("ns", Text, primary_key=True),
    Column("deposits_since_run", Integer, nullable=False, server_default="0"),
    Column("last_deposit_at", Float, nullable=True),
    Column("last_run_at", Float, nullable=True),
    Column("context_pressure", Float, nullable=False, server_default="0"),
    Column("last_maintenance_at", Float, nullable=True),
)
sleep_deferred = Table(
    "sleep_deferred",
    side_metadata,
    Column("ns", Text, primary_key=True),
    Column("kind", Text, primary_key=True),
    Column("stage", Text, primary_key=True),
    Column("pending", Integer, nullable=False),
    Column("total", Integer, nullable=False),
    Column("last_cycle", Integer, nullable=False),
)
sleep_fence_log = Table(
    "sleep_fence_log",
    side_metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("at", Float, nullable=False),
    Column("ns", Text, nullable=False),
    Column("op", Text, nullable=False),
    Column("presented_epoch", Integer, nullable=False),
    Column("current_epoch", Integer, nullable=True),
)
cost_entry = Table(
    "cost_entry",
    side_metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("run", Text, nullable=False),
    Column("ns_hash", Text, nullable=False),
    Column("layer", Text, nullable=False),
    Column("stage", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("model", Text, nullable=True),
    Column("calls", Integer, nullable=False),
    Column("tokens_in", Integer, nullable=False),
    Column("tokens_out", Integer, nullable=False),
    Column("latency_ms", Float, nullable=False),
    Column("outcome", Text, nullable=False),
)
dead_letters = Table(
    "dead_letters",
    side_metadata,
    Column("id", _ID, primary_key=True, autoincrement=True),
    Column("at", Float, nullable=False),
    Column("ns", Text, nullable=False),
    Column("window_id", Text, nullable=False),
    Column("kind", Text, nullable=False),
    Column("item_index", Integer, nullable=False),
    Column("code", Text, nullable=False),
    Column("detail", Text, nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("retry_allowed", Integer, nullable=False),
    Column("item_hash", Text, nullable=True),
    Column("content", Text, nullable=True),
    Index("dead_letters_ns", "ns", "id"),
)
side_file_imports = Table(
    "side_file_imports",
    side_metadata,
    Column("kind", Text, primary_key=True),
    Column("digest", Text, primary_key=True),
    Column("source", Text, nullable=False),
    Column("rows", Integer, nullable=False),
    Column("imported_at", Float, nullable=False),
)

#: The tables of each former side file (import order).
SIDE_TABLES: Mapping[str, tuple[str, ...]] = {
    "sleep": (
        "sleep_lease",
        "sleep_cycle",
        "sleep_watermark",
        "sleep_state",
        "sleep_deferred",
        "sleep_fence_log",
    ),
    "cost": ("cost_entry",),
    "deadletters": ("dead_letters",),
}
#: The former side file of each kind: ``<base><suffix>``.
SIDE_FILES: Mapping[str, str] = {
    "sleep": ".sleep",
    "cost": ".cost",
    "deadletters": ".deadletters.sqlite",
}
#: Append-only tables: their ``id`` is regenerated on import (rows have no natural key).
_APPEND_ONLY = frozenset({"sleep_fence_log", "cost_entry", "dead_letters"})


def is_postgres(engine: Engine) -> bool:
    return engine.dialect.name == "postgresql"


def local_engine(path: str) -> Engine:
    """A private SQLite engine for a store that has no authoritative database: ``:memory:`` (one
    shared connection) or a plain file (tests and tools only)."""
    if path == ":memory:":
        return create_engine(
            "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
        )
    return create_engine(
        f"sqlite:///{path}", connect_args={"timeout": 10.0, "check_same_thread": False}
    )


@contextlib.contextmanager
def write_tx(engine: Engine, *, lock: bool = False) -> Iterator[Connection]:
    """One write transaction: ``BEGIN IMMEDIATE`` on SQLite (the cross-process writer lock, taken at
    once so a read-then-write never upgrades), ``BEGIN`` on Postgres plus the advisory lock when
    ``lock`` (the ``BEGIN IMMEDIATE`` counterpart)."""
    with engine.connect() as con:
        if is_postgres(engine):
            con.begin()
            if lock:
                con.exec_driver_sql("SELECT pg_advisory_xact_lock(%s)", (ADVISORY_KEY,))
        else:
            con.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            yield con
        except BaseException:
            con.rollback()
            raise
        con.commit()


def ensure_side_tables(engine: Engine, names: Sequence[str] | None = None) -> None:
    """Create the side tables (``names``; all by default) that do not exist yet. Alembic normally
    made them already; this covers a ``create_all`` database. Serialised by the advisory lock on
    Postgres (concurrent ``CREATE`` of one table would race)."""
    tables = [side_metadata.tables[n] for n in (names or side_metadata.tables)]
    with write_tx(engine, lock=True) as con:
        side_metadata.create_all(con, tables=tables, checkfirst=True)


class BackgroundWriter:
    """Writes run on one worker thread, in submission order. ``drain`` waits for the queued ones;
    a failed write is logged (persistence never fails the caller that queued it)."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._pool: ThreadPoolExecutor | None = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=f"memspine-{name}"
        )
        self._pending: list[Future[Any]] = []
        self._lock = threading.Lock()

    def submit(self, fn: Callable[..., Any], *args: Any) -> None:
        with self._lock:
            if self._pool is None:
                fn(*args)  # closed: write inline rather than drop
                return
            self._pending = [f for f in self._pending if not f.done()]
            self._pending.append(self._pool.submit(self._run, fn, *args))

    def _run(self, fn: Callable[..., Any], *args: Any) -> None:
        try:
            fn(*args)
        except Exception as exc:
            _log.warning("side_table.write_failed", store=self._name, error=type(exc).__name__)

    def drain(self) -> None:
        with self._lock:
            pending, self._pending = self._pending, []
        for fut in pending:
            fut.result()

    def close(self) -> None:
        self.drain()
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=True)


# ---------------------------------------------------------------------------------- the import
@dataclass(frozen=True)
class SideImport:
    """One former side file found at start: its rows were copied (``rows``; 0 when this content was
    imported before) and it was renamed to ``renamed_to``."""

    kind: str
    source: str
    renamed_to: str
    rows: int
    already_imported: bool


def _read_legacy(
    path: Path, tables: Sequence[str], open_file: FileOpener
) -> dict[str, tuple[list[str], list[tuple[Any, ...]]]]:
    """Every row of the known tables of one side file, in insertion order, restricted to the
    columns the authoritative tables have. A table the old file lacks is skipped."""
    out: dict[str, tuple[list[str], list[tuple[Any, ...]]]] = {}
    with open_file(path) as con:
        present = {
            str(r[0]) for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for name in tables:
            if name not in present:
                continue
            have = {str(r[1]) for r in con.execute(f"PRAGMA table_info({name})")}
            cols = [
                c.name
                for c in side_metadata.tables[name].columns
                if c.name in have and not (name in _APPEND_ONLY and c.name == "id")
            ]
            order = " ORDER BY rowid"
            rows = con.execute(f"SELECT {', '.join(cols)} FROM {name}{order}").fetchall()
            out[name] = (cols, [tuple(r) for r in rows])
    return out


def _digest(kind: str, data: Mapping[str, tuple[list[str], list[tuple[Any, ...]]]]) -> str:
    blob = json.dumps(
        {"kind": kind, "tables": {t: [c, r] for t, (c, r) in sorted(data.items())}},
        sort_keys=True,
        default=str,
        separators=(",", ":"),
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _free_name(path: Path) -> Path:
    target = path.with_name(path.name + ".imported")
    n = 1
    while target.exists():
        target = path.with_name(f"{path.name}.imported.{n}")
        n += 1
    return target


def _copy_rows(con: Connection, data: Mapping[str, tuple[list[str], list[tuple[Any, ...]]]]) -> int:
    copied = 0
    for name, (cols, rows) in data.items():
        if not rows:
            continue
        values = ", ".join(f":{c}" for c in cols)
        sql = f"INSERT INTO {name} ({', '.join(cols)}) VALUES ({values})"
        if name not in _APPEND_ONLY:  # keyed rows: what the database already has wins
            sql += " ON CONFLICT DO NOTHING"
        for row in rows:
            copied += con.execute(text(sql), dict(zip(cols, row, strict=True))).rowcount or 0
    return copied


def import_side_files(
    engine: Engine,
    base: str,
    *,
    open_file: FileOpener,
    clock: Callable[[], float] = time.time,
) -> list[SideImport]:
    """The one-time import of ``<base>.sleep``, ``<base>.cost`` and ``<base>.deadletters.sqlite``.

    Idempotent: the rows and a digest of the file's content commit in one transaction, and a
    content already recorded is not copied again (a crash between the commit and the rename only
    leaves the rename to do). Each imported file is renamed to ``*.imported`` (never overwritten:
    ``.imported.1`` and so on). A file that cannot be read is logged and left in place."""
    if base == ":memory:":
        return []
    done: list[SideImport] = []
    for kind, suffix in SIDE_FILES.items():
        path = Path(f"{base}{suffix}")
        if not path.is_file():
            continue
        try:
            data = _read_legacy(path, SIDE_TABLES[kind], open_file)
        except sqlite3.Error as exc:
            _log.warning(
                "side_file.import_skipped", kind=kind, path=str(path), error=type(exc).__name__
            )
            continue
        digest = _digest(kind, data)
        ensure_side_tables(engine, (*SIDE_TABLES[kind], "side_file_imports"))
        with write_tx(engine, lock=True) as con:
            seen = con.execute(
                text("SELECT rows FROM side_file_imports WHERE kind=:k AND digest=:d"),
                {"k": kind, "d": digest},
            ).first()
            rows = 0
            if seen is None:
                rows = _copy_rows(con, data)
                con.execute(
                    text(
                        "INSERT INTO side_file_imports (kind, digest, source, rows, imported_at) "
                        "VALUES (:k, :d, :s, :r, :t)"
                    ),
                    {"k": kind, "d": digest, "s": str(path), "r": rows, "t": clock()},
                )
        target = _free_name(path)
        try:
            path.rename(target)
        except FileNotFoundError:  # another process renamed it first
            continue
        for extra in ("-wal", "-shm", "-journal"):
            leftover = Path(f"{path}{extra}")
            if leftover.exists():
                with contextlib.suppress(OSError):
                    leftover.rename(Path(f"{target}{extra}"))
        _log.info(
            "side_file.imported",
            kind=kind,
            path=str(path),
            renamed_to=str(target),
            rows=rows,
            already_imported=seen is not None,
        )
        done.append(SideImport(kind, str(path), str(target), rows, seen is not None))
    return done
