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

RM-111 WP5 (ADR-081): the tables live in the authoritative storage database, reached through its
sync SQLAlchemy engine (:mod:`memspine.store.side_tables`): encrypted with it on SQLite/SQLCipher,
shared by every process on it on Postgres. One short transaction per call, so two processes on one
database never overlap: ``BEGIN IMMEDIATE`` on SQLite, and on Postgres one advisory lock per
transaction (its counterpart); the few SQLite-only spellings are translated by :func:`pg_sql`. An
engine-backed store sets ``offload``: its callers run the writes on a worker thread, because the
event loop must never wait on SQLite's write lock (the async storage layer can hold it across an
await). A path (``:memory:``, or a plain SQLite file for tests and tools) keeps the stdlib
``sqlite3`` store; a Postgres DSN string still opens its own psycopg3 connection.

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
import functools
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from memspine.derive.budget import DerivationBudget, is_model_stage
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

from memspine.store.leases import (
    LeaseLost,
    LeaseStore,
    NamespaceLease,
    SleepJobError,
    StageWatermark,
    is_pg_url,
    pg_sql,
)

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
        self.budget = budget  # O4-01b (offline.budget); None = no budget, nothing below runs

    async def _io(self, fn: Callable[..., Any], *args: Any) -> Any:
        """One store write: on a worker thread for the storage database (RM-111 WP5: the loop must
        never wait on SQLite's write lock), inline for a private ``:memory:`` or file store."""
        if getattr(self.store, "offload", False):
            return await asyncio.to_thread(fn, *args)
        return fn(*args)

    async def run(self) -> JobOutcome:
        if self.cancelled():
            return JobOutcome("cancelled")
        lease = await self._io(self.store.claim, self.ns, self.owner, self.job_id, self.ttl)
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
            await self._io(self.store.release, lease)
            tap.event("job.lease", ns=self.ns, op="release", job=self.job_id, why="failed")
            return JobOutcome("failed", error=f"{type(exc).__name__}: {exc}")
        # simulated process death (any other BaseException) propagates with the lease still held
        await self._io(self.store.release, lease)
        tap.event("job.lease", ns=self.ns, op="release", job=self.job_id, why=outcome.state)
        return outcome

    async def _drive(self, lease: NamespaceLease) -> JobOutcome:
        opened = await self._io(self.store.open_cycle, lease, self.kind)
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
            beat = await self._io(self.store.heartbeat, lease, self.ttl)
            if beat is None:
                raise LeaseLost(self.ns)
            lease = beat
            if self.budget is not None and is_model_stage(stage) and self.budget.exhausted(self.ns):
                if not await self._io(self.store.defer_stage, lease, self.kind, cycle, stage):
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
                if stage in waiting and not await self._io(
                    self.store.drain_stage, lease, self.kind, stage
                ):
                    raise LeaseLost(self.ns)
            mark = await self._io(
                self.store.mark_stage, lease, self.kind, cycle, stage, self.head_seq()
            )
            if mark is None:
                raise LeaseLost(self.ns)
            tap.event(
                "job.watermark", ns=self.ns, kind=self.kind, cycle=cycle, stage=stage, seq=mark.seq
            )
        finish = functools.partial(self.store.finish_cycle, full=self.full)
        if not await self._io(finish, lease, self.kind, cycle):
            raise LeaseLost(self.ns)
        out.state = "succeeded"
        if out.deferred:  # deferred units count toward N (O-05), so the next trigger fires normally
            await self._io(self.store.carry_deferred, self.ns, len(out.deferred))
        return out


# ---------------------------------------------------------------------------------------------
# the handle


class SleepHandle:
    """Returned immediately by ``sleep()`` under ``offline.sleep``; never blocks (OF-1)."""

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
