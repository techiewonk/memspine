"""Namespace leases and stage watermarks for the sleep job (moved from derive/job.py).

The lease store is storage: it lives in the authoritative database (side tables, RM-111
WP5) and is used by the records port of the storage engines, so it sits in the store
layer, below the pipelines (target-layers contract).
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from sqlalchemy.engine import Engine as SqlAlchemyEngine

from memspine.exceptions import MemspineError, StorageError
from memspine.store.side_tables import SIDE_TABLES, ensure_side_tables

__all__ = [
    "Clock",
    "LeaseLost",
    "LeaseStore",
    "NamespaceLease",
    "SleepJobError",
    "StageWatermark",
    "is_pg_url",
    "pg_sql",
]


Clock = Callable[[], float]  # seconds since the epoch


class SleepJobError(MemspineError):
    """``SleepHandle.wait()`` on a job that failed or was cancelled."""


class LeaseLost(Exception):
    """The holder's epoch is no longer current: another process took the lease over."""


@dataclass(frozen=True)
class NamespaceLease:
    ns: str
    owner: str
    epoch: int
    job_id: str
    lease_until: float
    heartbeat_at: float


@dataclass(frozen=True)
class StageWatermark:
    ns: str
    kind: str
    cycle: int
    stage: str
    seq: int  # highest event seq the cycle had seen when the stage committed (0 = unknown)
    done_at: float


_SCHEMA = """
CREATE TABLE IF NOT EXISTS sleep_lease (
  ns TEXT PRIMARY KEY, owner TEXT NOT NULL, epoch INTEGER NOT NULL, job_id TEXT NOT NULL,
  lease_until REAL NOT NULL, heartbeat_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS sleep_cycle (
  ns TEXT NOT NULL, kind TEXT NOT NULL, cycle INTEGER NOT NULL, status TEXT NOT NULL,
  PRIMARY KEY (ns, kind));
CREATE TABLE IF NOT EXISTS sleep_watermark (
  ns TEXT NOT NULL, kind TEXT NOT NULL, cycle INTEGER NOT NULL, stage TEXT NOT NULL,
  seq INTEGER NOT NULL, done_at REAL NOT NULL, PRIMARY KEY (ns, kind, cycle, stage));
CREATE TABLE IF NOT EXISTS sleep_state (
  ns TEXT PRIMARY KEY, deposits_since_run INTEGER NOT NULL DEFAULT 0, last_deposit_at REAL,
  last_run_at REAL, context_pressure REAL NOT NULL DEFAULT 0, last_maintenance_at REAL);
CREATE TABLE IF NOT EXISTS sleep_deferred (
  ns TEXT NOT NULL, kind TEXT NOT NULL, stage TEXT NOT NULL, pending INTEGER NOT NULL,
  total INTEGER NOT NULL, last_cycle INTEGER NOT NULL, PRIMARY KEY (ns, kind, stage));
CREATE TABLE IF NOT EXISTS sleep_fence_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, ns TEXT NOT NULL, op TEXT NOT NULL,
  presented_epoch INTEGER NOT NULL, current_epoch INTEGER);
"""


#: Advisory-lock key every Postgres transaction of the store takes (the ``BEGIN IMMEDIATE`` role).
_PG_LOCK_KEY = 0x4D535053


def is_pg_url(path: str) -> bool:
    return path.startswith(("postgresql://", "postgres://", "postgresql+psycopg://"))


def pg_sql(sql: str) -> str:
    """The SQLite spelling of one store statement in Postgres: ``?`` placeholders, ``INSERT OR
    REPLACE`` / ``INSERT OR IGNORE`` as ``ON CONFLICT``, ``MAX(a, b)`` as ``GREATEST``, and the
    schema types ``REAL`` / ``AUTOINCREMENT``."""
    out = sql
    if "INSERT OR REPLACE INTO sleep_watermark" in out:  # the only REPLACE
        out = out.replace("INSERT OR REPLACE INTO", "INSERT INTO") + (
            " ON CONFLICT (ns, kind, cycle, stage) DO UPDATE "
            "SET seq=EXCLUDED.seq, done_at=EXCLUDED.done_at"
        )
    elif "INSERT OR IGNORE INTO" in out:
        out = out.replace("INSERT OR IGNORE INTO", "INSERT INTO") + " ON CONFLICT DO NOTHING"
    out = out.replace("MAX(pending,1)", "GREATEST(pending,1)")
    out = out.replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
    out = out.replace(" REAL", " DOUBLE PRECISION")
    return out.replace("?", "%s")


class _PgConn:
    """A psycopg3 connection offering the few ``sqlite3.Connection`` calls the store makes."""

    def __init__(self, url: str) -> None:
        try:
            import psycopg
        except ImportError as exc:
            raise StorageError("Postgres sleep leases need the [postgres] extra (psycopg)") from exc
        dsn = url.replace("postgresql+psycopg://", "postgresql://", 1)
        self._c = psycopg.connect(dsn, autocommit=True)
        self.in_transaction = False

    def execute(self, sql: str, params: Sequence[object] = ()) -> Any:
        head = sql.strip().upper()
        if head == "BEGIN IMMEDIATE":
            self._c.execute("BEGIN")
            self._c.execute("SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,))
            self.in_transaction = True
            return None
        if head in ("COMMIT", "ROLLBACK"):
            self.in_transaction = False
        return self._c.execute(pg_sql(sql), tuple(params))

    def executescript(self, script: str) -> None:
        self.execute("BEGIN IMMEDIATE")  # concurrent CREATE ... IF NOT EXISTS must not race
        try:
            for stmt in script.split(";"):
                if stmt.strip():
                    self.execute(stmt)
        except BaseException:
            self.execute("ROLLBACK")
            raise
        self.execute("COMMIT")

    def close(self) -> None:
        self._c.close()


class _SqlAlchemyConn:
    """Small DB-API-shaped view of an engine connection used by lease SQL."""

    def __init__(self, connection: Any, *, postgres: bool) -> None:
        self._connection = connection
        self._postgres = postgres

    @property
    def in_transaction(self) -> bool:
        return bool(self._connection.in_transaction())

    def execute(self, sql: str, params: Sequence[object] = ()) -> Any:
        head = sql.strip().upper()
        if head == "BEGIN IMMEDIATE":
            if self._postgres:
                self._connection.begin()
                self._connection.exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(%s)", (_PG_LOCK_KEY,)
                )
            else:  # SQLite: take the writer lock at once (the DB-API would begin deferred)
                self._connection.exec_driver_sql("BEGIN IMMEDIATE")
            return None
        if head == "COMMIT":
            self._connection.commit()
            return None
        if head == "ROLLBACK":
            self._connection.rollback()
            return None
        statement = pg_sql(sql) if self._postgres else sql
        return self._connection.exec_driver_sql(statement, tuple(params))

    def executescript(self, script: str) -> None:
        self.execute("BEGIN IMMEDIATE")
        try:
            for statement in script.split(";"):
                if statement.strip():
                    self.execute(statement)
        except BaseException:
            self.execute("ROLLBACK")
            raise
        self.execute("COMMIT")

    def close(self) -> None:
        self._connection.close()


class LeaseStore:
    """Persisted lease, cycle, watermark and trigger-state tables: in the storage database (a
    SQLAlchemy engine, SQLite or Postgres; RM-111 WP5), or ``:memory:`` / a plain file path."""

    def __init__(
        self, path: str | SqlAlchemyEngine = ":memory:", *, clock: Clock | None = None
    ) -> None:
        self._engine = path if isinstance(path, SqlAlchemyEngine) else None
        self.path: str
        if self._engine is not None:
            self.path = self._engine.url.render_as_string(hide_password=True)
        else:
            assert isinstance(path, str)
            self.path = path
        self.clock: Clock = clock or time.time
        if self._engine is not None:
            self._pg = self._engine.dialect.name == "postgresql"
        else:
            self._pg = is_pg_url(self.path)
        #: True for the storage database: callers on the event loop run the writes on a thread.
        self.offload = self._engine is not None
        self._db_time = self._pg and clock is None  # FU-GAPS: lease times come from the database
        self._shared: sqlite3.Connection | None = None
        if path == ":memory:":
            self._shared = sqlite3.connect(":memory:", isolation_level=None)
        if self._engine is not None:
            ensure_side_tables(self._engine, SIDE_TABLES["sleep"])
        else:
            with self._conn() as con:
                con.executescript(_SCHEMA)

    # ---------------------------------------------------------------- plumbing
    @contextlib.contextmanager
    def _conn(self) -> Iterator[Any]:
        if self._engine is not None:
            connection = self._engine.connect()
            try:
                yield _SqlAlchemyConn(connection, postgres=self._pg)
            finally:
                connection.close()
            return
        if self._shared is not None:
            yield self._shared
            return
        if self._pg:
            pg = _PgConn(self.path)
            try:
                yield pg
            finally:
                pg.close()
            return
        con = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        try:
            yield con
        finally:
            con.close()

    def close(self) -> None:
        if self._shared is not None:
            self._shared.close()
            self._shared = None

    @contextlib.contextmanager
    def _tx(self) -> Iterator[Any]:
        """One ``BEGIN IMMEDIATE`` transaction (cross-process writer lock)."""
        with self._conn() as con:
            con.execute("BEGIN IMMEDIATE")
            try:
                yield con
            except BaseException:
                if con.in_transaction:
                    con.execute("ROLLBACK")
                raise
            else:
                con.execute("COMMIT")

    def fence_log(self) -> list[tuple[str, str, int, int | None]]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT ns, op, presented_epoch, current_epoch FROM sleep_fence_log ORDER BY id"
            ).fetchall()
        return [
            (str(r[0]), str(r[1]), int(r[2]), None if r[3] is None else int(r[3])) for r in rows
        ]

    def _refuse(self, con: Any, lease: NamespaceLease, op: str) -> None:
        cur = con.execute("SELECT epoch FROM sleep_lease WHERE ns=?", (lease.ns,)).fetchone()
        con.execute(
            "INSERT INTO sleep_fence_log (at, ns, op, presented_epoch, current_epoch) "
            "VALUES (?,?,?,?,?)",
            (self.clock(), lease.ns, op, lease.epoch, None if cur is None else int(cur[0])),
        )

    def _lease_now(self, con: Any) -> float:
        """The time lease expiry is judged by: the database clock on Postgres (no host skew), else
        this process's clock."""
        if self._db_time:
            row = con.execute("SELECT EXTRACT(EPOCH FROM clock_timestamp())").fetchone()
            return float(row[0])
        return self.clock()

    def lease_state(self, ns: str) -> tuple[NamespaceLease | None, float | None]:
        """The namespace lease and the database's current time in one round trip (None on SQLite,
        where the caller's own clock is the right one)."""
        with self._conn() as con:
            r = con.execute(
                "SELECT ns, owner, epoch, job_id, lease_until, heartbeat_at "
                "FROM sleep_lease WHERE ns=?",
                (ns,),
            ).fetchone()
            now = self._lease_now(con) if self._db_time else None
        return (None if r is None else NamespaceLease(*r)), now

    @staticmethod
    def _current(con: Any, lease: NamespaceLease) -> bool:
        r = con.execute("SELECT epoch FROM sleep_lease WHERE ns=?", (lease.ns,)).fetchone()
        return r is not None and int(r[0]) == lease.epoch

    # ---------------------------------------------------------------- lease
    def get_lease(self, ns: str) -> NamespaceLease | None:
        with self._conn() as con:
            r = con.execute(
                "SELECT ns, owner, epoch, job_id, lease_until, heartbeat_at "
                "FROM sleep_lease WHERE ns=?",
                (ns,),
            ).fetchone()
        return None if r is None else NamespaceLease(*r)

    def claim(self, ns: str, owner: str, job_id: str, ttl: float) -> NamespaceLease | None:
        """Take the namespace lease if it is absent or expired; every takeover bumps the epoch."""
        with self._tx() as con:
            now = self._lease_now(con)
            r = con.execute(
                "SELECT epoch, lease_until FROM sleep_lease WHERE ns=?", (ns,)
            ).fetchone()
            if r is not None and r[1] > now:
                return None
            epoch = 1 if r is None else int(r[0]) + 1
            con.execute(
                "INSERT INTO sleep_lease (ns, owner, epoch, job_id, lease_until, heartbeat_at) "
                "VALUES (?,?,?,?,?,?) ON CONFLICT(ns) DO UPDATE SET owner=excluded.owner, "
                "epoch=excluded.epoch, job_id=excluded.job_id, "
                "lease_until=excluded.lease_until, heartbeat_at=excluded.heartbeat_at",
                (ns, owner, epoch, job_id, now + ttl, now),
            )
        return NamespaceLease(ns, owner, epoch, job_id, now + ttl, now)

    def heartbeat(self, lease: NamespaceLease, ttl: float) -> NamespaceLease | None:
        with self._tx() as con:
            now = self._lease_now(con)
            cur = con.execute(
                "UPDATE sleep_lease SET lease_until=?, heartbeat_at=? WHERE ns=? AND epoch=?",
                (now + ttl, now, lease.ns, lease.epoch),
            )
            if cur.rowcount == 0:
                self._refuse(con, lease, "heartbeat")
                return None
        return NamespaceLease(lease.ns, lease.owner, lease.epoch, lease.job_id, now + ttl, now)

    def release(self, lease: NamespaceLease) -> bool:
        """Free the lease (``lease_until`` = 0); the epoch stays so the next claim bumps it."""
        with self._tx() as con:
            cur = con.execute(
                "UPDATE sleep_lease SET lease_until=0 WHERE ns=? AND epoch=?",
                (lease.ns, lease.epoch),
            )
            if cur.rowcount == 0:
                self._refuse(con, lease, "release")
                return False
        return True

    # ---------------------------------------------------------------- cycles and watermarks
    def open_cycle(self, lease: NamespaceLease, kind: str) -> tuple[int, bool] | None:
        """The cycle to run: the unfinished one (resume) or a new one. Fenced (None when stale)."""
        with self._tx() as con:
            if not self._current(con, lease):
                self._refuse(con, lease, "open_cycle")
                return None
            r = con.execute(
                "SELECT cycle, status FROM sleep_cycle WHERE ns=? AND kind=?", (lease.ns, kind)
            ).fetchone()
            if r is not None and r[1] == "open":
                return int(r[0]), True
            cycle = 1 if r is None else int(r[0]) + 1
            con.execute(
                "INSERT INTO sleep_cycle (ns, kind, cycle, status) VALUES (?,?,?, 'open') "
                "ON CONFLICT(ns, kind) DO UPDATE SET cycle=excluded.cycle, status='open'",
                (lease.ns, kind, cycle),
            )
            return cycle, False

    def mark_stage(
        self, lease: NamespaceLease, kind: str, cycle: int, stage: str, seq: int = 0
    ) -> StageWatermark | None:
        """Advance the stage watermark. Refused (and logged) when the epoch is stale."""
        now = self.clock()
        with self._tx() as con:
            if not self._current(con, lease):
                self._refuse(con, lease, "mark_stage")
                return None
            con.execute(
                "INSERT OR REPLACE INTO sleep_watermark (ns, kind, cycle, stage, seq, done_at) "
                "VALUES (?,?,?,?,?,?)",
                (lease.ns, kind, cycle, stage, seq, now),
            )
        return StageWatermark(lease.ns, kind, cycle, stage, seq, now)

    def watermarks(self, ns: str, kind: str, cycle: int) -> list[StageWatermark]:
        with self._conn() as con:
            rows = con.execute(
                "SELECT ns, kind, cycle, stage, seq, done_at FROM sleep_watermark "
                "WHERE ns=? AND kind=? AND cycle=? ORDER BY done_at, stage",
                (ns, kind, cycle),
            ).fetchall()
        return [StageWatermark(*r) for r in rows]

    def finish_cycle(self, lease: NamespaceLease, kind: str, cycle: int, *, full: bool) -> bool:
        """Close the cycle (fenced) and stamp the trigger state (``full``: a complete sleep)."""
        now = self.clock()
        with self._tx() as con:
            if not self._current(con, lease):
                self._refuse(con, lease, "finish_cycle")
                return False
            con.execute(
                "UPDATE sleep_cycle SET status='done' WHERE ns=? AND kind=? AND cycle=?",
                (lease.ns, kind, cycle),
            )
            con.execute(
                "DELETE FROM sleep_watermark WHERE ns=? AND kind=? AND cycle=?",
                (lease.ns, kind, cycle),
            )
            con.execute("INSERT OR IGNORE INTO sleep_state (ns) VALUES (?)", (lease.ns,))
            if full:  # a full sleep includes decay, compress and retention
                con.execute(
                    "UPDATE sleep_state SET deposits_since_run=0, context_pressure=0, "
                    "last_run_at=?, last_maintenance_at=? WHERE ns=?",
                    (now, now, lease.ns),
                )
            else:
                con.execute(
                    "UPDATE sleep_state SET last_maintenance_at=? WHERE ns=?", (now, lease.ns)
                )
        return True

    # ---------------------------------------------------------------- deferred work (O4-01b)
    def defer_stage(self, lease: NamespaceLease, kind: str, cycle: int, stage: str) -> bool:
        """Record one stage as deferred by the budget (O-05). Idempotent per cycle: a resumed cycle
        does not count the same deferral twice. ``pending`` is how many cycles in a row the stage
        has waited, ``total`` how many deferrals it has had in all. Fenced."""
        with self._tx() as con:
            if not self._current(con, lease):
                self._refuse(con, lease, "defer_stage")
                return False
            r = con.execute(
                "SELECT last_cycle FROM sleep_deferred WHERE ns=? AND kind=? AND stage=?",
                (lease.ns, kind, stage),
            ).fetchone()
            if r is None:
                con.execute(
                    "INSERT INTO sleep_deferred (ns, kind, stage, pending, total, last_cycle) "
                    "VALUES (?,?,?,1,1,?)",
                    (lease.ns, kind, stage, cycle),
                )
            elif int(r[0]) != cycle:
                con.execute(
                    "UPDATE sleep_deferred SET pending=pending+1, total=total+1, last_cycle=? "
                    "WHERE ns=? AND kind=? AND stage=?",
                    (cycle, lease.ns, kind, stage),
                )
            else:  # the same cycle again: make sure it is pending, count nothing
                con.execute(
                    "UPDATE sleep_deferred SET pending=MAX(pending,1) "
                    "WHERE ns=? AND kind=? AND stage=?",
                    (lease.ns, kind, stage),
                )
        return True

    def drain_stage(self, lease: NamespaceLease, kind: str, stage: str) -> bool:
        """A deferred stage ran: it is no longer pending (``total`` keeps the history). Fenced."""
        with self._tx() as con:
            if not self._current(con, lease):
                self._refuse(con, lease, "drain_stage")
                return False
            con.execute(
                "UPDATE sleep_deferred SET pending=0 WHERE ns=? AND kind=? AND stage=?",
                (lease.ns, kind, stage),
            )
        return True

    def deferred(self, ns: str, kind: str = "sleep") -> dict[str, dict[str, int]]:
        """Per stage: ``pending`` (cycles waited, 0 = drained) and ``total`` deferrals ever."""
        with self._conn() as con:
            rows = con.execute(
                "SELECT stage, pending, total, last_cycle FROM sleep_deferred "
                "WHERE ns=? AND kind=? ORDER BY stage",
                (ns, kind),
            ).fetchall()
        return {
            str(r[0]): {"pending": int(r[1]), "total": int(r[2]), "last_cycle": int(r[3])}
            for r in rows
        }

    def carry_deferred(self, ns: str, n: int) -> None:
        """Deferred units count toward the deposit trigger N (O-05), so the next fire is normal."""
        if n <= 0:
            return
        with self._tx() as con:
            con.execute("INSERT OR IGNORE INTO sleep_state (ns) VALUES (?)", (ns,))
            con.execute(
                "UPDATE sleep_state SET deposits_since_run=deposits_since_run+? WHERE ns=?",
                (n, ns),
            )

    # ---------------------------------------------------------------- trigger state
    def bump_deposits(self, ns: str, n: int = 1) -> None:
        now = self.clock()
        with self._tx() as con:
            # a new namespace's first maintenance is due one interval after its first deposit
            con.execute(
                "INSERT OR IGNORE INTO sleep_state (ns, last_maintenance_at) VALUES (?,?)",
                (ns, now),
            )
            con.execute(
                "UPDATE sleep_state SET deposits_since_run=deposits_since_run+?, "
                "last_deposit_at=? WHERE ns=?",
                (n, now, ns),
            )

    def set_pressure(self, ns: str, value: float) -> None:
        now = self.clock()
        with self._tx() as con:
            con.execute(
                "INSERT OR IGNORE INTO sleep_state (ns, last_maintenance_at) VALUES (?,?)",
                (ns, now),
            )
            con.execute("UPDATE sleep_state SET context_pressure=? WHERE ns=?", (value, ns))

    def state(self, ns: str) -> dict[str, float | int | None] | None:
        with self._conn() as con:
            r = con.execute(
                "SELECT deposits_since_run, last_deposit_at, last_run_at, context_pressure, "
                "last_maintenance_at FROM sleep_state WHERE ns=?",
                (ns,),
            ).fetchone()
        if r is None:
            return None
        return {
            "deposits_since_run": int(r[0]),
            "last_deposit_at": r[1],
            "last_run_at": r[2],
            "context_pressure": float(r[3]),
            "last_maintenance_at": r[4],
        }

    def namespaces(self) -> list[str]:
        with self._conn() as con:
            return [str(r[0]) for r in con.execute("SELECT ns FROM sleep_state ORDER BY ns")]


# ---------------------------------------------------------------------------------------------
# the job
