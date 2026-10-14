"""Inline runner over the durable outbox (F-04): drains after each write, fences every commit,
never holds a namespace lock across a model call, and gives reads a barrier.

The outbox (``runtime.outbox``) is the truth; this runner is only the default executor (owner Q2):
a taskiq transport replaces it later (RT-01) without touching the queue. Per claimed job it walks
the offsets ``(watermark, target]`` in order. Each offset runs in two phases:

``prepare``  everything slow, the model calls included. NO lock is held.
``commit``   the deposit. With ``StageHandler.commit_locked`` the runner takes the namespace lock
             (``commit_lock(namespace)``) around this phase only; a handler whose deposits already
             lock per record (the window pipeline) leaves it False.

After every offset the watermark is checkpointed under the lease epoch (``JobBook.checkpoint``), so
a stop or a crash resumes at the next offset. An offset whose handler raised becomes a hole (never
skipped silently) and is retried on later drains inside the per-hole budget. A stale epoch is
refused and logged; the stale worker stops without writing.
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from memspine.derive.backpressure import (
    UNDERIVED_KEY,
    Backlog,
    Backpressure,
    BackpressureRejected,
)
from memspine.economy.slo import SloLedger
from memspine.models.concurrency import Priority, priority_scope
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.runtime.freshness import read_barrier
from memspine.runtime.jobs import Claim, Clock, Job, JobBook, JobStore, RetryBudget
from memspine.runtime.outbox import SqlJobStore
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.public import JobHandle

__all__ = [
    "BarrierOutcome",
    "DrainReport",
    "InlineRunner",
    "StageHandler",
    "utc_now",
]

_log = get_logger(__name__)

Prepare = Callable[[OwnerScope, int, dict[str, Any]], Awaitable[Any]]
Commit = Callable[[OwnerScope, int, Any], Awaitable[None]]
NamespaceLock = Callable[[str], AbstractAsyncContextManager[Any]]
Sleeper = Callable[[float], Awaitable[None]]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class StageHandler:
    """What the runner executes for one outbox stage (a capability, in read-barrier terms)."""

    prepare: Prepare
    commit: Commit
    commit_locked: bool = False


@dataclass
class DrainReport:
    jobs: int = 0
    items: int = 0
    holes: int = 0
    hole_retries: int = 0
    hole_filled: int = 0
    fenced: int = 0
    released: int = 0
    stopped: bool = False


@dataclass(frozen=True)
class BarrierOutcome:
    """Result of ``InlineRunner.wait``: ``ready`` or ``partial`` (never ``wait``)."""

    status: Literal["ready", "partial"]
    targets: Mapping[str, int]
    ready: tuple[str, ...] = ()
    pending: tuple[str, ...] = ()
    unavailable: tuple[str, ...] = ()
    holes: Mapping[str, tuple[int, ...]] = field(default_factory=dict)

    @property
    def missing(self) -> tuple[str, ...]:
        return tuple(sorted({*self.pending, *self.unavailable}))


class _Fenced(Exception):
    """A commit or checkpoint was refused: this worker no longer owns the job."""


class _Stop(Exception):
    """The runner is stopping: hand the job back at the next safe point."""


class InlineRunner:
    """Drains outbox jobs in the calling process. See the module docstring."""

    def __init__(
        self,
        store: JobStore,
        *,
        clock: Clock = utc_now,
        owner: str = "inline",
        lease_ttl: timedelta = timedelta(minutes=5),
        budget: RetryBudget | None = None,
        commit_lock: NamespaceLock | None = None,
        sleeper: Sleeper | None = None,
        poll_s: float = 0.02,
    ) -> None:
        if not isinstance(store, SqlJobStore):
            raise TypeError("InlineRunner needs a SqlJobStore (items live beside the jobs)")
        self.store: SqlJobStore = store
        self.clock = clock
        self.owner = owner
        self.lease_ttl = lease_ttl
        self.book = JobBook(store, clock, budget or RetryBudget(base_backoff=timedelta(0)))
        self._commit_lock = commit_lock
        self._handlers: dict[str, StageHandler] = {}
        self._sleep: Sleeper = sleeper or asyncio.sleep
        self._poll_s = poll_s
        self._drain_lock = asyncio.Lock()
        self._book_lock = threading.Lock()  # one book mutation at a time (they are multi-call)
        self._stopping = False
        self._current: Claim | None = None
        self._retry_holes_next = True
        #: RT-02 (``runtime.scheduler_v2``): both None = unchanged behaviour
        self._backpressure: Backpressure | None = None
        self._ledger: SloLedger | None = None

    # ------------------------------------------------------------------ setup
    def register(
        self,
        stage: str,
        handler: StageHandler | None = None,
        *,
        prepare: Prepare | None = None,
        commit: Commit | None = None,
        commit_locked: bool = False,
    ) -> None:
        """Register a stage: a ``StageHandler``, or its ``prepare``/``commit`` callables (so callers
        below ``runtime`` in the layering, such as ``workers.windows``, need not import it)."""
        if handler is None:
            if prepare is None or commit is None:
                raise TypeError("register needs a StageHandler or both prepare and commit")
            handler = StageHandler(prepare=prepare, commit=commit, commit_locked=commit_locked)
        self._handlers[stage] = handler

    @property
    def stages(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def configure_scheduler(
        self, backpressure: Backpressure | None, ledger: SloLedger | None = None
    ) -> None:
        """RT-02: apply a backpressure policy at enqueue, run items as background derive work,
        and record time-to-searchable in ``ledger``. Called once at build (flag on only)."""
        self._backpressure = backpressure
        self._ledger = ledger

    # ------------------------------------------------------------------ enqueue
    async def _io(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        """Run one book or store mutation on a worker thread, under the book lock.

        The event loop must never block on the database: the storage layer can hold SQLite's write
        lock across an await (a transaction between two statements) and its next step needs this
        loop, so a sync write here would wait out the busy timeout for a lock only the loop frees.
        Reads (progress, depth, items) stay direct: WAL readers never wait for a writer."""

        def run() -> Any:
            with self._book_lock:
                return fn(*args, **kw)

        return await asyncio.to_thread(run)

    def _enqueue(
        self, scope: OwnerScope, stage: str, payload: dict[str, Any], force: bool = False
    ) -> tuple[JobHandle, int]:
        if self._backpressure is not None:
            payload = self._admit(scope, stage, payload, force)
        offset = self.store.put_item(scope, stage, payload, self.clock())
        return self.book.enqueue(scope, stage, offset), offset

    def _admit(
        self, scope: OwnerScope, stage: str, payload: dict[str, Any], force: bool = False
    ) -> dict[str, Any]:
        """Backpressure (W-25 B): reject before accepting, or coalesce and mark underived.
        Raises ``BackpressureRejected`` with nothing written; never drops accepted work."""
        bp = self._backpressure
        assert bp is not None
        items, oldest = self.store.backlog(scope, stage)
        now = self.clock()
        age = 0.0 if oldest is None else max(0.0, (now - oldest).total_seconds())
        total = self.store.backlog()[0] if bp.max_total_items else items
        try:
            verdict = bp.admit(f"{scope.tenant}/{scope.owner}", stage, Backlog(items, age), total)
        except BackpressureRejected as exc:
            if force:  # an explicit flush: accept past the hard cap, marked underived
                tap.event("runtime.backpressure.forced", stage=stage, items=items)
                return {**payload, UNDERIVED_KEY: True}
            receipt = exc.receipt
            if self._ledger is not None:
                self._ledger.note_rejected()
            tap.event(
                "runtime.backpressure.reject",
                stage=stage,
                reason=receipt.reason,
                items=items,
                oldest_age_s=round(age, 3),
                retry_after_s=receipt.retry_after_s,
            )
            raise
        if self._ledger is not None:
            self._ledger.set_backlog(items + 1, age)
        if verdict.decision == "coalesce":
            if self._ledger is not None:
                self._ledger.note_coalesced()
            tap.event(
                "runtime.backpressure.coalesce",
                stage=stage,
                reason=verdict.reason,
                items=items,
                oldest_age_s=round(age, 3),
            )
            return {**payload, UNDERIVED_KEY: True}
        return payload

    async def enqueue(
        self, scope: OwnerScope, stage: str, payload: dict[str, Any], *, force: bool = False
    ) -> JobHandle:
        """Persist one item and ask the book for a job covering it. Returns the job handle, which
        is ``coalesced`` when an existing queued or running job already covers the offset."""
        handle, offset = await self._io(self._enqueue, scope, stage, payload, force)
        tap.event(
            "runtime.outbox.enqueue",
            job_id=handle.job_id,
            stage=stage,
            offset=offset,
            state=handle.state,
            coalesced_into=handle.coalesced_into,
        )
        return handle

    # ------------------------------------------------------------------ budget parking (W2-12)
    @staticmethod
    def parked_stage(stage: str) -> str:
        """The outbox stage that holds work a spent budget paused (it has no handler, so nothing
        drains it; the rows stay as persisted underived markers until ``resume_parked``)."""
        return f"{stage}.deferred"

    async def park(self, scope: OwnerScope, stage: str, payload: dict[str, Any]) -> bool:
        """Hold one item back, marked underived (W-26 D). Idempotent: an identical payload that
        is already parked is not parked twice (a crash between park and checkpoint redoes it).
        Returns whether a row was written."""
        return bool(await self._io(self._park, scope, stage, payload))

    def _park(self, scope: OwnerScope, stage: str, payload: dict[str, Any]) -> bool:
        held = {**payload, UNDERIVED_KEY: True}
        parked = self.parked_stage(stage)
        if any(p == held for _, p in self.store.items_after(scope, parked, -1)):
            return False
        self.store.put_item(scope, parked, held, self.clock())
        return True

    def parked(self, scope: OwnerScope, stage: str) -> int:
        """How many items of ``stage`` wait, paused by a budget."""
        return len(self.store.items_after(scope, self.parked_stage(stage), -1))

    async def resume_parked(self, scope: OwnerScope, stage: str) -> int:
        """Enqueue every parked item of ``stage`` again, in order, then clear the parked rows.
        Enqueue first and prune after, so a crash in between duplicates (derivation is
        idempotent) and never loses. Returns the number re-enqueued."""
        parked = self.parked_stage(stage)
        items = self.store.items_after(scope, parked, -1)
        for _, payload in items:
            await self.enqueue(scope, stage, payload, force=True)  # past a hard cap, never dropped
        if items:
            await self._io(self.store.prune_items, scope, parked, items[-1][0])
        return len(items)

    def depth(self, scope: OwnerScope | None = None) -> int:
        return self.book.queue_depth(scope)

    def pending_items(self, scope: OwnerScope, stage: str) -> list[tuple[int, dict[str, Any]]]:
        """Items beyond the watermark (not yet handled), oldest first."""
        return self.store.items_after(scope, stage, self.book.progress(scope, stage).watermark)

    async def rewrite_pending(
        self,
        scope: OwnerScope,
        stage: str,
        fn: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> int:
        """Rewrite the payload of every unhandled item through ``fn`` (None: leave it). Used by
        forget so no queued item still names an erased record. Returns the rewritten count."""
        return int(await self._io(self._rewrite_pending, scope, stage, fn))

    def _rewrite_pending(
        self,
        scope: OwnerScope,
        stage: str,
        fn: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> int:
        n = 0
        for offset, payload in self.pending_items(scope, stage):
            new = fn(payload)
            if new is not None:
                self.store.set_item(scope, stage, offset, new)
                n += 1
        return n

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> DrainReport:
        """Resume after a stop or a crash: expired leases are reclaimed and leftovers drained."""
        self._stopping = False
        return await self.drain()

    async def stop(self, deadline_s: float = 5.0) -> DrainReport:
        """Bounded drain (ER-10): let the offset in hand finish for up to ``deadline_s``, then give
        the job back (``release``: no attempt spent). Queued work stays in the outbox, so nothing
        is lost; the next ``start`` resumes at the watermark."""
        self._stopping = True
        report = DrainReport(stopped=True)
        deadline = time.monotonic() + max(deadline_s, 0.0)
        while self._drain_lock.locked() and time.monotonic() < deadline:
            await self._sleep(self._poll_s)
        if self._drain_lock.locked():
            report.released += await self._release_current()
        tap.event(
            "runtime.outbox.drain",
            phase="stop",
            released=report.released,
            queued=self.depth(),
        )
        return report

    async def _release_current(self) -> int:
        claim = self._current
        return await self._release(claim) if claim is not None else 0

    async def _release(self, claim: Claim) -> int:
        """Give a claimed job back unless it already is (a stop past its deadline releases it
        first, and the cancelled worker then arrives here too)."""
        return int(await asyncio.shield(self._io(self._release_sync, claim)))

    def _release_sync(self, claim: Claim) -> int:
        job = self.store.get_job(claim.job.job_id)
        if job is None or job.state != "running" or job.epoch != claim.lease.epoch:
            return 0
        return 1 if self.book.release(job.job_id, claim.lease.epoch).ok else 0

    # ------------------------------------------------------------------ drain
    async def drain(self, scope: OwnerScope | None = None) -> DrainReport:
        """Run every ready job (of ``scope``, or all) to completion, then retry open holes."""
        report = DrainReport()
        async with self._drain_lock:
            for stage in self._handlers:
                await self._drain_stage(scope, stage, report)
        if report.jobs or report.items:
            tap.event(
                "runtime.outbox.drain",
                phase="drain",
                jobs=report.jobs,
                items=report.items,
                holes=report.holes,
                fenced=report.fenced,
            )
        return report

    async def _drain_stage(self, scope: OwnerScope | None, stage: str, report: DrainReport) -> None:
        if self._retry_holes_next:
            await self._retry_holes(scope, stage, report)
        while not self._stopping:
            claim = await self._io(
                self.book.claim, self.owner, self.lease_ttl, scope=scope, stage=stage
            )
            if claim is None:
                return
            self._current = claim
            tap.event(
                "runtime.outbox.claim",
                job_id=claim.job.job_id,
                stage=stage,
                epoch=claim.lease.epoch,
                target_offset=claim.job.target_offset,
            )
            try:
                await self._run_claim(claim, report)
            except _Fenced:
                report.fenced += 1
            except _Stop:
                report.stopped = True
                report.released += await self._release(claim)
                return
            except asyncio.CancelledError:
                # cancelled mid-item (stop past its deadline): the job goes back, nothing is lost
                report.released += await self._release(claim)
                raise
            finally:
                self._current = None
            report.jobs += 1

    async def _item(
        self, handler: StageHandler, scope: OwnerScope, stage: str, offset: int
    ) -> None:
        payload = self.store.get_item(scope, stage, offset)
        if payload is None:  # pruned or never written: nothing to do, not an error
            return
        if self._backpressure is None and self._ledger is None:  # flag off: the original path
            await self._phases(handler, scope, offset, payload, stage)
            return
        with priority_scope(Priority.DERIVE):  # RT-02: background derive work
            await self._phases(handler, scope, offset, payload, stage)
        if self._ledger is not None:
            queued_at = self.store.item_created_at(scope, stage, offset)
            if queued_at is not None:
                self._ledger.record_lag(max(0.0, (self.clock() - queued_at).total_seconds()))

    async def _phases(
        self,
        handler: StageHandler,
        scope: OwnerScope,
        offset: int,
        payload: dict[str, Any],
        stage: str = "",
    ) -> None:
        if insp.inspect_enabled():  # TI-1 (F17): prepare and commit run inside a job request
            with tap.request("job", stage=f"outbox_{stage}", offset=offset):
                with tap.span("runtime.outbox.prepare", stage=stage, offset=offset):
                    prepared = await handler.prepare(scope, offset, payload)
                with tap.span("runtime.outbox.commit_item", stage=stage, offset=offset):
                    await self._commit(handler, scope, offset, prepared)
            return
        prepared = await handler.prepare(scope, offset, payload)
        await self._commit(handler, scope, offset, prepared)

    async def _commit(
        self, handler: StageHandler, scope: OwnerScope, offset: int, prepared: Any
    ) -> None:
        if handler.commit_locked and self._commit_lock is not None:
            async with self._commit_lock(scope.owner):
                await handler.commit(scope, offset, prepared)
        else:
            await handler.commit(scope, offset, prepared)

    async def _run_claim(self, claim: Claim, report: DrainReport) -> None:
        job, epoch = claim.job, claim.lease.epoch
        handler = self._handlers[job.stage]
        start = self.book.progress(job.scope, job.stage).watermark + 1
        for offset in range(start, job.target_offset + 1):
            if self._stopping:
                raise _Stop
            if not (await self._io(self.book.heartbeat, job.job_id, epoch, self.lease_ttl)).ok:
                raise _Fenced
            failed: dict[int, tuple[str, str | None]] = {}
            try:
                await self._item(handler, job.scope, job.stage, offset)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failed[offset] = ("raised", f"{type(exc).__name__}: {exc}"[:300])
                report.holes += 1
                tap.event(
                    "runtime.outbox.hole",
                    job_id=job.job_id,
                    stage=job.stage,
                    offset=offset,
                    reason="raised",
                    error=type(exc).__name__,
                )
                _log.warning(
                    "outbox.item_failed", job_id=job.job_id, offset=offset, error=str(exc)[:200]
                )
            if not (await self._io(self.book.checkpoint, job.job_id, epoch, offset, failed)).ok:
                raise _Fenced
            report.items += 1
        res = await self._io(self.book.commit, job.job_id, epoch)
        if not res.ok:
            raise _Fenced
        tap.event(
            "runtime.outbox.commit",
            job_id=job.job_id,
            stage=job.stage,
            epoch=epoch,
            through=job.target_offset,
        )

    async def _retry_holes(self, scope: OwnerScope | None, stage: str, report: DrainReport) -> None:
        """One retry pass over open holes still inside the per-hole budget (ER-09, ER-15)."""
        handler = self._handlers[stage]
        for sc in self._scopes_with_holes(scope, stage):
            for hole in self.book.holes_to_retry(sc, stage):
                if self._stopping:
                    return
                report.hole_retries += 1
                try:
                    await self._item(handler, sc, stage, hole.offset)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    await self._io(
                        self.book.record_hole_result,
                        sc,
                        stage,
                        hole.offset,
                        False,
                        f"{type(exc).__name__}: {exc}"[:300],
                    )
                else:
                    await self._io(self.book.record_hole_result, sc, stage, hole.offset, True)
                    report.hole_filled += 1

    def _scopes_with_holes(self, scope: OwnerScope | None, stage: str) -> list[OwnerScope]:
        if scope is not None:
            return [scope]
        seen: dict[tuple[str, str, str], OwnerScope] = {}
        for j in self.store.jobs():
            if j.stage == stage:
                seen.setdefault((j.scope.tenant, j.scope.owner, j.scope.owner_kind), j.scope)
        return list(seen.values())

    # ------------------------------------------------------------------ read barrier
    async def wait(
        self,
        scope: OwnerScope,
        capabilities: list[str] | tuple[str, ...],
        timeout: float | timedelta = 0.0,  # noqa: ASYNC109 - the barrier budget
    ) -> BarrierOutcome:
        """Read barrier (session 7): resolve once every requested capability's watermark reaches
        the offset enqueued when this call began (later writes do not extend the target), or give
        up at ``timeout`` and return ``partial`` naming what is missing. A capability with no
        registered handler is ``unavailable`` and never waited for. While work is pending and no
        drain runs, the caller drains inline (that is the inline runner's contract)."""
        limit = timeout if isinstance(timeout, timedelta) else timedelta(seconds=timeout)
        started = self.clock()
        targets = {c: self.store.last_offset(scope, c) for c in capabilities}
        tap.event(
            "runtime.barrier.wait",
            phase="begin",
            capabilities=sorted(capabilities),
            timeout_s=limit.total_seconds(),
        )
        while True:
            outcome, waiting = self._barrier(scope, targets, limit, started)
            if waiting and not self._drain_lock.locked() and not self._stopping:
                await self.drain(scope)
                outcome, waiting = self._barrier(scope, targets, limit, started)
            if not waiting:
                tap.event(
                    "runtime.barrier.wait",
                    phase="end",
                    status=outcome.status,
                    missing=list(outcome.missing),
                    holes=sum(len(v) for v in outcome.holes.values()),
                )
                return outcome
            await self._sleep(self._poll_s)

    def _barrier(
        self, scope: OwnerScope, targets: Mapping[str, int], limit: timedelta, started: datetime
    ) -> tuple[BarrierOutcome, bool]:
        ready: list[str] = []
        pending: list[str] = []
        unavailable: list[str] = []
        holes: dict[str, tuple[int, ...]] = {}
        waiting = False
        for cap, target in targets.items():
            if cap not in self._handlers:
                unavailable.append(cap)
                continue
            progress = self.store.get_progress(((scope.tenant, scope.owner, scope.owner_kind), cap))
            res = read_barrier(
                [cap],
                target,
                {cap: progress},
                timeout=limit,
                clock=self.clock,
                started_at=started,
            )
            if res.holes.get(cap):
                holes[cap] = res.holes[cap]
            if res.status == "ready":
                ready.append(cap)
            else:
                pending.append(cap)
                waiting = waiting or res.status == "wait"
        status: Literal["ready", "partial"] = "partial" if (pending or unavailable) else "ready"
        outcome = BarrierOutcome(
            status, dict(targets), tuple(ready), tuple(pending), tuple(unavailable), holes
        )
        return outcome, waiting

    def close(self) -> None:
        self.store.close()

    # ------------------------------------------------------------------ introspection
    def job(self, job_id: str) -> Job:
        return self.book.get(job_id)
