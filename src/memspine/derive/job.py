"""O4-01a: the namespace sleep job, its lease and its stage watermarks (O-03 B).

* :class:`NamespaceLease` is a persisted row with an expiry. A claim succeeds only when no lease
  exists or the existing one has expired, and every takeover bumps a monotonic ``epoch`` (ER-07).
  Every later write by the holder (heartbeat, watermark, finish, release) is conditional on its
  epoch still being current, so a stale holder cannot advance state (it is *fenced*).
* :class:`StageWatermark` marks one stage of one cycle complete. A cycle stays open until every
  stage committed; the next job for that namespace resumes the open cycle and skips the stages
  already marked, so a restart repeats no completed stage. A stage that was running when the
  process died has no mark and runs again (stage effects are idempotent, O-03).
* :class:`SleepHandle` is what a non-blocking ``sleep()`` returns: ``status()``, ``wait()``,
  ``cancel()``.

The store is a small SQLite file (stdlib ``sqlite3``, one short transaction per call), so two
processes on one database never overlap. On the Postgres backend (``storage.url``; FU-SLEEP) the
same tables and the same SQL run through psycopg3, so processes on different hosts never overlap
either: the few SQLite-only spellings are translated by :func:`pg_sql`, and every transaction takes
one advisory lock, the counterpart of the SQLite ``BEGIN IMMEDIATE`` writer lock.

Clock skew (FU-GAPS). Lease expiry is compared against a clock. On SQLite (one host) that is the
process clock. On Postgres, processes on different hosts would each judge expiry by their own clock,
so a fast host could take a live lease over; there the store reads the DATABASE clock
(``clock_timestamp()``, inside the claim or heartbeat transaction) for every lease time it writes
or compares, and :meth:`LeaseStore.lease_state` hands the same clock to a waiting scheduler. An
injected ``clock`` (tests) always wins. The outbox job book (``runtime.jobs``) keeps its own
injected clock; it is not part of this change.

``derive`` may not import ``runtime``: this module reuses the *concepts* of
``runtime/outbox.py`` and ``runtime/jobs.py`` (lease, epoch, watermark, fence log) and shares
only ``memspine.schemas`` value types with them.
"""

from __future__ import annotations

import asyncio
import contextlib
import sqlite3
import time
from collections.abc import Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from memspine.derive.budget import DerivationBudget, is_model_stage
from memspine.exceptions import MemspineError, StorageError
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.schemas.public import JobHandle
from memspine.schemas.public.receipt import JobState

__all__ = [
    "JobOutcome",
    "LeaseLost",
    "LeaseStore",
    "NamespaceJob",
    "NamespaceLease",
    "SleepHandle",
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


class LeaseStore:
    """Persisted lease, cycle, watermark and trigger-state tables (SQLite file, or ``:memory:``;
    a Postgres DSN keeps them in the storage database, shared by every process on it)."""

    def __init__(self, path: str = ":memory:", *, clock: Clock | None = None) -> None:
        self.path = path
        self.clock: Clock = clock or time.time
        self._pg = is_pg_url(path)
        self._db_time = self._pg and clock is None  # FU-GAPS: lease times come from the database
        self._shared: sqlite3.Connection | None = None
        if path == ":memory:":
            self._shared = sqlite3.connect(":memory:", isolation_level=None)
        with self._conn() as con:
            con.executescript(_SCHEMA)

    # ---------------------------------------------------------------- plumbing
    @contextlib.contextmanager
    def _conn(self) -> Iterator[Any]:
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

RunStage = Callable[[str], Awaitable[dict[str, object]]]


@dataclass
class JobOutcome:
    """How a job ended. ``state`` is a public ``JobState``; ``holder`` names the job that held the
    lease when this one was fenced (state ``coalesced``)."""

    state: JobState
    stats: dict[str, dict[str, object]] = field(default_factory=dict)
    resumed: bool = False
    skipped: list[str] = field(default_factory=list)
    holder: str | None = None
    error: str | None = None
    cycle: int = 0
    #: O4-01b: stages the budget deferred this cycle (counted and persisted, run next cycle).
    deferred: list[str] = field(default_factory=list)


class NamespaceJob:
    """One namespace's stage-by-stage pass under a lease, with per-stage watermarks (O-03 B).

    ``run`` claims the lease (a ``coalesced`` outcome when another job holds it), opens the
    unfinished cycle or a new one, runs the stages not yet watermarked, advances the watermark
    after each, heartbeats between stages, and finishes the cycle. A stage that raises
    ``Exception`` fails the job (the lease is released, the cycle stays open for the next run).
    ``CancelledError`` and ``cancelled()`` release the lease and leave the cycle open. Any other
    ``BaseException`` models a dead process: nothing is released, the lease simply expires.
    """

    def __init__(
        self,
        store: LeaseStore,
        ns: str,
        stages: Sequence[str],
        run_stage: RunStage,
        *,
        kind: str = "sleep",
        owner: str,
        job_id: str,
        ttl: float = 300.0,
        full: bool = True,
        prepare: Callable[[], Awaitable[None]] | None = None,
        cancelled: Callable[[], bool] = lambda: False,
        head_seq: Callable[[], int] = lambda: 0,
        budget: DerivationBudget | None = None,
    ) -> None:
        self.store, self.ns, self.stages, self.kind = store, ns, tuple(stages), kind
        self.run_stage, self.owner, self.job_id, self.ttl = run_stage, owner, job_id, ttl
        self.full, self.prepare, self.cancelled, self.head_seq = full, prepare, cancelled, head_seq
        self.budget = budget  # O4-01b (offline.budget_v2); None = no budget, nothing below runs

    async def run(self) -> JobOutcome:
        if self.cancelled():
            return JobOutcome("cancelled")
        lease = self.store.claim(self.ns, self.owner, self.job_id, self.ttl)
        if lease is None:
            held = self.store.get_lease(self.ns)
            holder = held.job_id if held is not None else None
            tap.event("job.lease", ns=self.ns, op="fenced", job=self.job_id, holder=holder)
            return JobOutcome("coalesced", holder=holder)
        tap.event("job.lease", ns=self.ns, op="claim", job=self.job_id, epoch=lease.epoch)
        try:
            outcome = await self._drive(lease)
        except asyncio.CancelledError:
            self.store.release(lease)
            tap.event("job.lease", ns=self.ns, op="release", job=self.job_id, why="cancelled")
            raise
        except LeaseLost:
            tap.event("job.lease", ns=self.ns, op="lost", job=self.job_id, epoch=lease.epoch)
            return JobOutcome("failed", error="lease lost (fenced by a newer epoch)")
        except Exception as exc:
            self.store.release(lease)
            tap.event("job.lease", ns=self.ns, op="release", job=self.job_id, why="failed")
            return JobOutcome("failed", error=f"{type(exc).__name__}: {exc}")
        # simulated process death (any other BaseException) propagates with the lease still held
        self.store.release(lease)
        tap.event("job.lease", ns=self.ns, op="release", job=self.job_id, why=outcome.state)
        return outcome

    async def _drive(self, lease: NamespaceLease) -> JobOutcome:
        opened = self.store.open_cycle(lease, self.kind)
        if opened is None:
            raise LeaseLost(self.ns)
        cycle, resumed = opened
        done = {w.stage for w in self.store.watermarks(self.ns, self.kind, cycle)}
        out = JobOutcome("running", resumed=resumed, cycle=cycle)
        if self.prepare is not None:
            await self.prepare()
        stages = self.stages
        waiting: dict[str, dict[str, int]] = {}
        if self.budget is not None:  # O4-01b: drain what the last cycle deferred first
            self.budget.begin(self.ns)
            waiting = {
                st: d for st, d in self.store.deferred(self.ns, self.kind).items() if d["pending"]
            }
            first = [st for st in stages if st in waiting]
            stages = (*first, *[st for st in stages if st not in waiting])
        for stage in stages:
            if stage in done:
                out.skipped.append(stage)
                out.stats[stage] = {"status": "resumed"}
                continue
            if self.cancelled():
                out.state = "cancelled"
                return out
            beat = self.store.heartbeat(lease, self.ttl)
            if beat is None:
                raise LeaseLost(self.ns)
            lease = beat
            if self.budget is not None and is_model_stage(stage) and self.budget.exhausted(self.ns):
                if not self.store.defer_stage(lease, self.kind, cycle, stage):
                    raise LeaseLost(self.ns)
                out.deferred.append(stage)
                out.stats[stage] = {"status": "deferred", "reason": "budget"}
                row = self.store.deferred(self.ns, self.kind).get(stage, {})
                tap.event(
                    "job.deferred",
                    ns=self.ns,
                    kind=self.kind,
                    cycle=cycle,
                    stage=stage,
                    pending=row.get("pending", 1),
                    total=row.get("total", 1),
                )
                continue
            with insp.sleep_cycle(cycle):  # TI-1: job.stage.item carries the cycle number
                out.stats[stage] = await self.run_stage(stage)
            if self.budget is not None:
                spent = self.budget.charge(self.ns, out.stats[stage])
                tap.event(
                    "job.budget",
                    ns=self.ns,
                    cycle=cycle,
                    stage=stage,
                    calls=spent.calls,
                    tokens=spent.tokens,
                    max_calls=self.budget.max_calls,
                    max_tokens=self.budget.max_tokens,
                    exhausted=self.budget.exhausted(self.ns),
                )
                if stage in waiting and not self.store.drain_stage(lease, self.kind, stage):
                    raise LeaseLost(self.ns)
            mark = self.store.mark_stage(lease, self.kind, cycle, stage, self.head_seq())
            if mark is None:
                raise LeaseLost(self.ns)
            tap.event(
                "job.watermark", ns=self.ns, kind=self.kind, cycle=cycle, stage=stage, seq=mark.seq
            )
        if not self.store.finish_cycle(lease, self.kind, cycle, full=self.full):
            raise LeaseLost(self.ns)
        out.state = "succeeded"
        if out.deferred:  # deferred units count toward N (O-05), so the next trigger fires normally
            self.store.carry_deferred(self.ns, len(out.deferred))
        return out


# ---------------------------------------------------------------------------------------------
# the handle


class SleepHandle:
    """Returned immediately by ``sleep()`` under ``offline.sleep_v2``; never blocks (OF-1)."""

    def __init__(self, job_id: str, ns: str, kind: str = "sleep") -> None:
        self.job_id, self.ns, self.kind = job_id, ns, kind
        self._state: JobState = "queued"
        self._holder: str | None = None
        self._error: str | None = None
        self._cancel = False
        self._done = asyncio.Event()
        self._outcome: JobOutcome | None = None
        self._task: asyncio.Task[None] | None = None
        #: set by the scheduler: wait for the lease holder this handle was fenced behind
        self._wait_holder: Callable[[float | None], Awaitable[None]] | None = None

    # -- called by the scheduler
    def _set(self, outcome: JobOutcome) -> None:
        self._outcome = outcome
        self._state = outcome.state
        self._holder = outcome.holder
        self._error = outcome.error
        self._done.set()
        tap.event(
            "job.sleep.handle", job=self.job_id, ns=self.ns, state=self._state, holder=self._holder
        )

    def _running(self) -> None:
        if self._state == "queued":
            self._state = "running"
            tap.event("job.sleep.handle", job=self.job_id, ns=self.ns, state="running")

    # -- public
    def status(self) -> JobHandle:
        return JobHandle(
            job_id=self.job_id,
            kind=self.kind,
            state=self._state,
            coalesced_into=self._holder if self._state == "coalesced" else None,
            error=self._error if self._state == "failed" else None,
        )

    @property
    def done(self) -> bool:
        return self._done.is_set()

    async def wait(
        self,
        timeout: float | None = None,  # noqa: ASYNC109 - same shape as asyncio.wait_for
    ) -> dict[str, dict[str, object]]:
        """The stage stats of a finished job. A fenced (coalesced) handle waits for the holder to
        release the lease and returns ``{}``. Raises :class:`SleepJobError` on failure or cancel."""
        await asyncio.wait_for(self._done.wait(), timeout)
        if self._state == "coalesced" and self._wait_holder is not None:
            await self._wait_holder(timeout)
        if self._state == "failed":
            raise SleepJobError(self._error or "sleep job failed")
        if self._state == "cancelled":
            raise SleepJobError("sleep job cancelled")
        return {} if self._outcome is None else self._outcome.stats

    def cancel(self) -> bool:
        """Ask the job to stop. A queued job never starts; a running one stops at the next stage
        boundary (the cycle stays open, so the next sleep resumes it). False when already over."""
        if self._done.is_set():
            return False
        self._cancel = True
        tap.event("job.sleep.handle", job=self.job_id, ns=self.ns, state="cancel_requested")
        return True

    def __repr__(self) -> str:  # pragma: no cover
        return f"SleepHandle({self.job_id!r}, ns={self.ns!r}, state={self._state!r})"
