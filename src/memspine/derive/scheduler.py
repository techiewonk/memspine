"""O4-01a: the derivation scheduler (O-01, O-02, O-03): when a namespace job runs and how.

``DerivationScheduler`` owns the trigger bookkeeping (deposit counts, idle timers, the minimum
gap), starts :class:`~memspine.derive.job.NamespaceJob` tasks and hands back a non-blocking
:class:`~memspine.derive.job.SleepHandle`. It is built by the engine only under
``offline.sleep_v2`` (default off); with the flag off nothing here is imported at run time.

It runs in the host process (G-19 A): triggers fire while a process is alive, once at open
(catch-up from the persisted counters), on each deposit commit and from a per-namespace idle timer.
``derive`` may not import ``runtime``; the engine passes the stage runner, the namespace lister and
the scheduling-priority switch in. With ``background_priority`` the job (and the model calls its
stages make) run in the ``DERIVE`` class of the RT-02 model gate.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import os
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from memspine.derive.budget import COVERS_SEQ, DerivationBudget
from memspine.derive.job import (
    JobOutcome,
    LeaseStore,
    NamespaceJob,
    SleepHandle,
)
from memspine.derive.triggers import TriggerDecision, TriggerPolicy, TriggerState
from memspine.models.concurrency import Priority, priority_scope
from memspine.observability import tap
from memspine.observability.logging import get_logger

__all__ = ["ALL_NAMESPACES", "DerivationScheduler", "in_derive_job"]

_log = get_logger(__name__)

#: The lease key of an engine-wide (manual, no namespace) sleep: today's stages sweep every
#: namespace, so a manual ``sleep()`` is one job over the whole store.
ALL_NAMESPACES = "*"

_IN_JOB: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "memspine_in_derive_job", default=False
)


def in_derive_job() -> bool:
    """True inside a sleep job's task: its own writes must not count as new deposits."""
    return _IN_JOB.get()


def _ts(value: float | int | None) -> datetime | None:
    return None if value is None else datetime.fromtimestamp(float(value), UTC)


class DerivationScheduler:
    """Per-namespace sleep triggers, jobs, leases and the daily maintenance pass."""

    def __init__(
        self,
        *,
        store: LeaseStore,
        policy: TriggerPolicy,
        run_stage: Callable[[str], Awaitable[dict[str, object]]],
        stages: Callable[[], Sequence[str]],
        maintenance_stages: Callable[[], Sequence[str]],
        prepare: Callable[[], Awaitable[None]] | None = None,
        lease_ttl: float = 300.0,
        background_priority: bool = False,
        idle_timers: bool = True,
        clock: Callable[[], float] = time.time,
        budget: DerivationBudget | None = None,
        covers_snapshot: Callable[[], Mapping[str, int]] | None = None,
    ) -> None:
        self.store, self.policy, self.clock = store, policy, clock
        self._run_stage, self._stages, self._maint = run_stage, stages, maintenance_stages
        self._prepare, self._ttl = prepare, lease_ttl
        self.budget = budget  # O4-01b (offline.budget_v2): caps model work per cycle per namespace
        self._covers_snapshot = covers_snapshot  # O4-01b: stamps covers_seq on derived records
        self.last_seq = 0  # highest committed event seq this process has seen
        self._background, self._idle_timers = background_priority, idle_timers
        self._owner = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self._handles: dict[str, SleepHandle] = {}
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._stopped = False

    # ------------------------------------------------------------------ jobs
    def sleep(self, ns: str | None = None, *, kind: str = "sleep") -> SleepHandle:
        """Start a namespace job and return at once. Manual verbs bypass the trigger policy but not
        the lease: a second job on a namespace that is already being slept is fenced (its handle
        becomes ``coalesced`` into the holder's job)."""
        key = ns or ALL_NAMESPACES
        handle = SleepHandle(f"{kind}-{uuid.uuid4().hex[:12]}", key, kind)
        self._handles[handle.job_id] = handle
        for old in [j for j, h in self._handles.items() if h.done][:-200]:
            del self._handles[old]  # keep the last 200 finished handles
        handle._wait_holder = lambda timeout, h=handle: self._wait_for_holder(h, timeout)
        task = asyncio.get_running_loop().create_task(self._drive(handle))
        handle._task = task
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        tap.event("job.sleep.handle", job=handle.job_id, ns=key, state="queued", kind=kind)
        return handle

    async def _drive(self, handle: SleepHandle) -> None:
        maint = handle.kind == "maintenance"
        job = NamespaceJob(
            self.store,
            handle.ns,
            list(self._maint() if maint else self._stages()),
            self._stage_runner,
            kind=handle.kind,
            owner=self._owner,
            job_id=handle.job_id,
            ttl=self._ttl,
            full=not maint,
            prepare=None if maint else self._prepare,
            cancelled=lambda: handle._cancel,
            head_seq=lambda: self.last_seq,
            budget=self.budget,
        )
        token = _IN_JOB.set(True)
        try:
            handle._running()
            scope = (
                priority_scope(Priority.DERIVE) if self._background else contextlib.nullcontext()
            )
            with scope:
                outcome = await job.run()
        except asyncio.CancelledError:
            handle._set(JobOutcome("cancelled"))
            raise
        except BaseException as exc:  # a simulated crash: report, then let it propagate
            handle._set(JobOutcome("failed", error=f"{type(exc).__name__}: {exc}"))
            raise
        finally:
            _IN_JOB.reset(token)
        handle._set(outcome)
        if outcome.state == "succeeded" and not maint:
            self._rearm(handle.ns)

    async def _stage_runner(self, stage: str) -> dict[str, object]:
        if self._covers_snapshot is None:
            return await self._run_stage(stage)
        token = COVERS_SEQ.set(self._covers_snapshot())  # what this stage will have read
        try:
            return await self._run_stage(stage)
        finally:
            COVERS_SEQ.reset(token)

    async def _wait_for_holder(
        self,
        handle: SleepHandle,
        timeout: float | None,  # noqa: ASYNC109
    ) -> None:
        """Wait until the lease this handle was fenced behind is released or has expired."""
        deadline = None if timeout is None else self.clock() + timeout
        holder = handle._holder
        local = self._handles.get(holder) if holder else None
        if local is not None:
            await asyncio.wait_for(local._done.wait(), timeout)
            return
        while True:  # held by another process: poll the lease row
            lease = self.store.get_lease(handle.ns)
            if lease is None or lease.lease_until <= self.clock():
                return
            if deadline is not None and self.clock() >= deadline:
                raise TimeoutError
            await asyncio.sleep(0.05)

    # ------------------------------------------------------------------ triggers
    def _trigger_state(self, ns: str) -> TriggerState | None:
        row = self.store.state(ns)
        if row is None:
            return None
        return TriggerState(
            deposits_since_run=int(row["deposits_since_run"] or 0),
            last_deposit_at=_ts(row["last_deposit_at"]),
            last_run_at=_ts(row["last_run_at"]),
            context_pressure=float(row["context_pressure"] or 0.0),
            last_maintenance_at=_ts(row["last_maintenance_at"]),
        )

    def on_commit(self, ns: str, seq: int = 0) -> None:
        """A deposit committed. Counts it (persisted, so a restart keeps the count) and arms the
        idle timer once N is met. Writes made by a sleep job itself do not count."""
        self.last_seq = max(self.last_seq, seq)
        if self._stopped or in_derive_job():
            return
        self.store.bump_deposits(ns)
        state = self._trigger_state(ns)
        if state is not None and state.deposits_since_run >= max(1, self.policy.cfg.min_turns):
            self._arm(ns, self.policy.cfg.idle.total_seconds())

    def note_pressure(self, ns: str, value: float) -> None:
        """Report the namespace's context pressure (working memory over budget, 0..1)."""
        self.store.set_pressure(ns, float(value))
        if value >= self.policy.cfg.context_pressure:
            self._arm(ns, 0.0)

    def evaluate(self, ns: str, *, now: datetime | None = None) -> TriggerDecision:
        """Run the trigger policy for one namespace; start a job when it fires (and the daily
        maintenance job when that is due and no sleep ran). Re-arms the timer when it must wait."""
        when = now or _ts(self.clock())
        assert when is not None
        state = self._trigger_state(ns)
        if state is None:
            return TriggerDecision(False, "none", "no_state")
        decision = self.policy.decide(state, when)
        due = self.policy.maintenance_due(state, when)
        tap.event(
            "job.trigger",
            ns=ns,
            fire=decision.fire,
            reason=decision.reason,
            deposits=state.deposits_since_run,
            pressure=state.context_pressure,
            maintenance_due=due,
        )
        if decision.fire:
            self.sleep(ns)
        elif due:
            self.sleep(ns, kind="maintenance")
        if not decision.fire and decision.next_check_at is not None:
            self._arm(ns, max(0.0, (decision.next_check_at - when).total_seconds()))
        return decision

    async def on_open(self) -> list[str]:
        """Start-up catch-up: evaluate every namespace with persisted counters (E8)."""
        fired = []
        for ns in self.store.namespaces():
            if self.evaluate(ns).fire:
                fired.append(ns)
        return fired

    # ------------------------------------------------------------------ timers
    def _arm(self, ns: str, delay: float) -> None:
        if not self._idle_timers or self._stopped:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        old = self._timers.pop(ns, None)
        if old is not None:
            old.cancel()
        self._timers[ns] = loop.call_later(delay, self._timer_fired, ns)

    def _rearm(self, ns: str) -> None:
        state = self._trigger_state(ns)
        if state is not None and state.deposits_since_run > 0:
            self._arm(ns, self.policy.cfg.idle.total_seconds())

    def _timer_fired(self, ns: str) -> None:
        self._timers.pop(ns, None)
        if not self._stopped:
            with contextlib.suppress(Exception):
                self.evaluate(ns)

    # ------------------------------------------------------------------ lifecycle
    def handle(self, job_id: str) -> SleepHandle | None:
        return self._handles.get(job_id)

    async def stop(self) -> None:
        """Cancel the timers and the running jobs (their leases are released, their cycles stay
        open and resume on the next run), then close the store."""
        self._stopped = True
        for timer in self._timers.values():
            timer.cancel()
        self._timers.clear()
        tasks = list(self._tasks)
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        self.store.close()

    def describe(self) -> dict[str, Any]:
        return {
            "owner": self._owner,
            "jobs": {j: h.status().state for j, h in self._handles.items()},
            "timers": sorted(self._timers),
        }
