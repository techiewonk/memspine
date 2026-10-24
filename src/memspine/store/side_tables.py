"""Current authoritative SQL side tables and serialized background writes."""

from __future__ import annotations

import contextlib
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
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
)
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.pool import StaticPool

__all__ = [
    "SIDE_TABLES",
    "BackgroundWriter",
    "ensure_side_tables",
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
