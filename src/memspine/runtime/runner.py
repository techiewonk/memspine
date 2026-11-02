"""Inline runner over the durable outbox (F-04): drains after each write, fences every commit,
never holds a namespace lock across a model call, and gives reads a barrier.

The outbox (``runtime.outbox``) is the truth; this runner is only the default executor (owner Q2):
a taskiq transport replaces it later (RT-01) without touching the queue. Per claimed job it walks
the offsets ``(watermark, target]`` in order. Each offset runs in two phases:

``prepare``    everything slow, the model calls included. NO lock is held.
``authorize`` atomically validates lease and source snapshots and appends durable effect events.
``commit``     consumes those events through idempotent provider deposits. With
               ``StageHandler.commit_locked`` the runner takes the namespace lock around this
               phase only; the window pipeline's deposits already lock per record.
``finalize``   acknowledges projection outcomes under the same lease and source fence. When a
               source was erased or changed while the provider projected (the projection holds
               no database lock), finalize refuses and the runner calls
               ``StageHandler.compensate`` to remove what it projected, then fences.

After every offset the watermark is checkpointed under the lease epoch (``JobBook.checkpoint``), so
a stop or a crash resumes at the next offset. An offset whose handler raised becomes a hole (never
skipped silently) and is retried on later drains inside the per-hole budget. A stale epoch is
refused and logged; the stale worker stops without writing.

Fences are classified (RM-147):

=================  =============  ==============================================================
fence              class          what the drain does
=================  =============  ==============================================================
lease              transient      the job is no longer ours; nothing is released or recorded
busy               transient      another consumer holds the effect events: hand the job back
source (stale)     transient      a snapshot changed, or a membership is not accepted yet:
                                  hand the job back; a later drain re-reads and recomputes
replay_diverged    deterministic  a replay planned effects that differ from the acknowledged
                                  ones: a recorded, waivable hole; nothing is (re)projected
source_missing     deterministic  the item still names an erased source, so its effect can
                                  never be authorized: a recorded, waivable hole
=================  =============  ==============================================================

A transient fence is never a hole: the offset is not checkpointed, the job is handed back when
this worker still owns it, and the same drain goes on with every OTHER (scope, stage) key; a later
drain retries the fenced one. A deterministic fence would be met again on every retry, so the
offset becomes a hole with that reason, a structured alert is logged, and the watermark moves
past it. Such holes are never retried automatically (``NON_RETRYABLE_HOLE_REASONS``); an owner
waives them, and an erasure rewrite of a ``source_missing`` item reopens it.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from memspine.config.constants import OUTBOX_CANCELLED_KEY as CANCELLED_KEY
from memspine.config.constants import OUTBOX_FINISHED_JOBS_KEPT
from memspine.derive.backpressure import (
    UNDERIVED_KEY,
    Backlog,
    Backpressure,
    BackpressureRejected,
)
from memspine.economy.slo import SloLedger
from memspine.observability import inspect as insp
from memspine.observability import tap
from memspine.observability.logging import get_logger
from memspine.runtime.freshness import read_barrier
from memspine.runtime.jobs import Claim, Clock, Job, JobBook, JobStore, RetryBudget
from memspine.runtime.outbox import EffectFence, SqlJobStore
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.ingestion import EffectIdentity, IngestionSourceSnapshot
from memspine.schemas.public import JobHandle
from memspine.services.concurrency import Priority, priority_scope

__all__ = [
    "BarrierOutcome",
    "DrainReport",
    "EffectResult",
    "InlineRunner",
    "StageHandler",
    "utc_now",
]

_log = get_logger(__name__)

Prepare = Callable[[OwnerScope, int, dict[str, Any]], Awaitable[Any]]
Commit = Callable[[OwnerScope, int, Any], Awaitable[None]]


@dataclass(frozen=True)
class EffectResult:
    """A provider projection outcome planned before its durable authorization."""

    count: int
    artifact: dict[str, Any] | None = None
    producer_revision: str = "base"
    policy_revision: str = "base"


EffectResults = Callable[[Any], Mapping[str, int | EffectResult | Mapping[str, Any]]]
#: ``compensate(scope, offset, prepared, effects)`` removes what ``commit`` projected for the
#: given effect plans (each carries ``effect_id``, ``layer`` and ``identity``). Called when the
#: fence fails after projection because a source was erased or changed meanwhile.
Compensate = Callable[[OwnerScope, int, Any, Sequence[dict[str, Any]]], Awaitable[None]]
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
    effect_results: EffectResults | None = None
    compensate: Compensate | None = None


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
    #: deterministic-fence holes recorded in this drain, by reason (RM-147)
    hole_fences: dict[str, int] = field(default_factory=dict)


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


#: Transient fences (RM-147): retrying later can succeed, so they never become holes.
FenceReason = Literal["lease", "source", "busy"]
#: Deterministic fences (RM-147): every retry meets them again, so they become holes.
HoleFenceReason = Literal["replay_diverged", "source_missing"]


class _Fenced(Exception):
    """A transient fence: this worker no longer owns the job (``lease``), a source snapshot is
    stale or not accepted yet (``source``), or another consumer holds the effect events
    (``busy``). Never a hole."""

    def __init__(self, reason: FenceReason = "lease") -> None:
        super().__init__(reason)
        self.reason: FenceReason = reason


class _DeterministicFence(Exception):
    """A fence every retry meets again: the offset becomes a recorded, waivable hole whose
    reason is ``replay_diverged`` or ``source_missing``. ``detail`` never names a record."""

    def __init__(self, reason: HoleFenceReason, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason: HoleFenceReason = reason
        self.detail = detail


class _ReplayDiverged(_DeterministicFence):
    """A replay planned effects that differ from the acknowledged ones (layer set or result)."""

    def __init__(self) -> None:
        super().__init__(
            "replay_diverged", "the replay planned effects that differ from the acknowledged ones"
        )


_FENCE_REASONS: dict[EffectFence, FenceReason] = {
    EffectFence.STALE_LEASE: "lease",
    EffectFence.STALE_SOURCE: "source",
    EffectFence.BUSY: "busy",
}


def _fence_for(outcome: EffectFence) -> _Fenced | _DeterministicFence:
    if outcome is EffectFence.REPLAY_DIVERGED:
        return _ReplayDiverged()
    return _Fenced(_FENCE_REASONS.get(outcome, "lease"))


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
        #: RT-02 (``runtime.scheduler``): both None = unchanged behaviour
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
        effect_results: EffectResults | None = None,
        compensate: Compensate | None = None,
    ) -> None:
        """Register a stage: a ``StageHandler``, or its ``prepare``/``commit`` callables (so callers
        below ``runtime`` in the layering, such as ``workers.windows``, need not import it)."""
        if handler is None:
            if prepare is None or commit is None:
                raise TypeError("register needs a StageHandler or both prepare and commit")
            handler = StageHandler(
                prepare=prepare,
                commit=commit,
                commit_locked=commit_locked,
                effect_results=effect_results,
                compensate=compensate,
            )
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
        return handle  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt

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

    async def save_draft(
        self, scope: OwnerScope, stage: str, payload: dict[str, Any] | None
    ) -> None:
        """Atomically replace incomplete stage work; ``None`` clears it."""
        await self._io(self.store.put_draft, scope, stage, payload, self.clock())

    async def update_draft(
        self,
        scope: OwnerScope,
        stage: str,
        upsert: Sequence[dict[str, Any]],
        remove: Sequence[tuple[str, str]],
    ) -> int:
        """RM-163: apply one draft delta atomically (only the turns that changed); returns
        the payload bytes written."""
        return int(
            await self._io(self.store.update_draft, scope, stage, upsert, remove, self.clock())
        )

    async def delete_draft_record(self, scope: OwnerScope, stage: str, record_id: str) -> int:
        """Delete every draft row of ``record_id`` (the forget purge)."""
        return int(await self._io(self.store.delete_draft_record, scope, stage, record_id))

    async def load_draft(self, scope: OwnerScope, stage: str) -> dict[str, Any] | None:
        """Load incomplete stage work saved before a process restart."""
        draft = await self._io(self.store.get_draft, scope, stage)
        return draft if isinstance(draft, dict) else None

    def effect_status(self, scope: OwnerScope) -> list[dict[str, Any]]:
        """Persisted per-layer results for readiness/status surfaces."""
        return self.store.effect_status(scope)

    def contains_record(self, scope: OwnerScope, stage: str, record_id: str) -> bool:
        """Whether durable queued or draft work already contains ``record_id``."""
        if self.store.has_source(scope, stage, record_id):
            return True
        for _offset, payload in self.store.items_after(scope, stage, -1):
            if record_id in payload.get("record_ids", []):
                return True
        return bool(self.store.draft_has_record(scope, stage, record_id))

    def source_snapshots(
        self, scope: OwnerScope, stage: str, record_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        """The membership snapshots the effect door fences on (empty when one is missing)."""
        return self.store.source_snapshots(scope, stage, record_ids)

    def has_source(self, scope: OwnerScope, stage: str, record_id: str) -> bool:
        """Whether ``record_id`` has an active (accepted, not erased) membership row."""
        return self.store.has_source(scope, stage, record_id)

    async def mark_sources(
        self, scope: OwnerScope, stage: str, sources: Sequence[dict[str, Any]]
    ) -> None:
        """Acknowledge versioned source snapshots accepted by the canonical stage."""
        await self._io(self.store.mark_sources, scope, stage, sources, self.clock())

    async def erase_source(self, scope: OwnerScope, stage: str, record_id: str) -> bool:
        """Advance the durable erasure fence before an in-flight worker may commit."""
        return bool(await self._io(self.store.erase_source, scope, stage, record_id))

    async def rewrite_pending(
        self,
        scope: OwnerScope,
        stage: str,
        fn: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> int:
        """Rewrite the payload of every unhandled item through ``fn`` (None: leave it). Used by
        forget so no queued item still names an erased record. Returns the rewritten count."""
        return int(await self._io(self._rewrite_pending, scope, stage, fn))

    async def rewrite_all_items(
        self,
        scope: OwnerScope,
        stage: str,
        fn: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> int:
        """Rewrite retained payloads too; erasure must remove historical source ids."""
        return int(await self._io(self._rewrite_all_items, scope, stage, fn))

    async def rewrite_all_stage_items(
        self, stage: str, fn: Callable[[dict[str, Any]], dict[str, Any] | None]
    ) -> int:
        """Rewrite retained payloads for every scope of a stage."""
        total = 0
        for scope in self.store.item_scopes(stage):
            total += await self.rewrite_all_items(scope, stage, fn)
        return total

    def _rewrite_all_items(
        self,
        scope: OwnerScope,
        stage: str,
        fn: Callable[[dict[str, Any]], dict[str, Any] | None],
    ) -> int:
        n = 0
        for offset, payload in self.store.items_after(scope, stage, -1):
            new = fn(payload)
            if new is not None:
                self.store.set_item(scope, stage, offset, new)
                n += 1
        return n

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
        # A ``source_missing`` hole is unhandled work too: a drain may have met the erased
        # source before this rewrite removed it. The rewritten item can proceed, so reopen it.
        for hole in self.book.progress(scope, stage).holes:
            if hole.reason != "source_missing" or hole.waived:
                continue
            held = self.store.get_item(scope, stage, hole.offset)
            rewritten = None if held is None else fn(held)
            if rewritten is not None:
                self.store.set_item(scope, stage, hole.offset, rewritten)
                self.book.reopen_hole(scope, stage, hole.offset, "reopened: item rewritten")
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
        await self._reconcile_stage(scope, stage)
        if self._retry_holes_next:
            await self._retry_holes(scope, stage, report)
        # (scope, stage) keys that met a transient fence in this call: skipped until the next
        # drain, so one fenced scope never ends the drain for the others (RM-147).
        fenced: set[tuple[tuple[str, str, str], str]] = set()
        while not self._stopping:
            claim = await self._io(
                self.book.claim,
                self.owner,
                self.lease_ttl,
                scope=scope,
                stage=stage,
                exclude=frozenset(fenced),
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
            except _Fenced as fence:
                report.fenced += 1
                tap.event(
                    "runtime.outbox.fenced",
                    job_id=claim.job.job_id,
                    stage=stage,
                    reason=fence.reason,
                )
                # Hand the job back when it is still ours (no attempt spent): the fenced offset
                # was not checkpointed, so a later drain retries it. Re-claiming it now would
                # meet the same fence at once (or spin on a lease another worker holds), so
                # this drain skips that key and goes on with the other scopes.
                report.released += await self._release(claim)
                fenced.add(claim.job.key)
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

    async def _reconcile_stage(self, scope: OwnerScope | None, stage: str) -> None:
        """Create missing jobs for atomically discovered durable items.

        RM-161: every drain (one per write in a burst) starts here; its store reads run on a
        worker thread (``_io``), so they never hold the event loop a concurrent read needs."""

        def behind() -> list[tuple[OwnerScope, int]]:
            scopes = [scope] if scope is not None else self.store.item_scopes(stage)
            out: list[tuple[OwnerScope, int]] = []
            for candidate in scopes:
                top = self.store.last_offset(candidate, stage)
                if top > self.book.progress(candidate, stage).watermark:
                    out.append((candidate, top))
            return out

        for candidate, top in await self._io(behind):
            await self._io(self.book.enqueue, candidate, stage, top)

    async def _item(self, handler: StageHandler, scope: OwnerScope, stage: str, offset: int) -> Any:
        payload = self.store.get_item(scope, stage, offset)
        if payload is None or payload.get(CANCELLED_KEY):  # pruned, never written or superseded
            return None
        if self._backpressure is None and self._ledger is None:  # flag off: the original path
            return await self._phases(handler, scope, offset, payload, stage)
        with priority_scope(Priority.DERIVE):  # RT-02: background derive work
            prepared = await self._phases(handler, scope, offset, payload, stage)
        if self._ledger is not None:
            queued_at = self.store.item_created_at(scope, stage, offset)
            if queued_at is not None:
                self._ledger.record_lag(max(0.0, (self.clock() - queued_at).total_seconds()))
        return prepared

    async def _phases(
        self,
        handler: StageHandler,
        scope: OwnerScope,
        offset: int,
        payload: dict[str, Any],
        stage: str = "",
    ) -> Any:
        if insp.inspect_enabled():  # TI-1 (F17): prepare and commit run inside a job request
            with tap.span("runtime.outbox.prepare", stage=stage, offset=offset):
                return await handler.prepare(scope, offset, payload)
        return await handler.prepare(scope, offset, payload)

    def _effect_plans(
        self,
        handler: StageHandler,
        job: Job,
        offset: int,
        prepared: Any,
        sources: Sequence[dict[str, Any]],
        effect_stage: str | None = None,
    ) -> list[dict[str, Any]]:
        if handler.effect_results is None:
            return []
        results = handler.effect_results(prepared)
        semantic_stage = effect_stage or job.stage
        snapshots = tuple(IngestionSourceSnapshot.model_validate(source) for source in sources)
        plans: list[dict[str, Any]] = []
        for layer, raw in results.items():
            if isinstance(raw, EffectResult):
                result = raw
            elif isinstance(raw, Mapping):
                result = EffectResult(
                    count=int(raw.get("count", 0)),
                    artifact=raw.get("artifact"),
                    producer_revision=str(raw.get("producer_revision", "base")),
                    policy_revision=str(raw.get("policy_revision", "base")),
                )
            else:
                result = EffectResult(count=raw)
            canonical_job = (
                f"{job.scope.tenant}/{job.scope.owner}/{job.scope.owner_kind}:"
                f"{semantic_stage}:{offset}"
            )
            if snapshots:
                identity_payload = EffectIdentity.build(
                    job_id=canonical_job,
                    output_key=layer,
                    sources=snapshots,
                    producer_revision=result.producer_revision,
                    policy_revision=result.policy_revision,
                ).model_dump(mode="json")
                effect_id = str(identity_payload["effect_id"])
            else:
                identity_payload = {
                    "job_id": canonical_job,
                    "output_key": layer,
                    "sources": [],
                    "producer_revision": result.producer_revision,
                    "policy_revision": result.policy_revision,
                }
                effect_id = hashlib.sha256(
                    json.dumps(identity_payload, sort_keys=True, separators=(",", ":")).encode()
                ).hexdigest()[:16]
                identity_payload["effect_id"] = effect_id
            plans.append(
                {
                    "layer": layer,
                    "effect_id": effect_id,
                    "identity": identity_payload,
                    "count": result.count,
                    "artifact": result.artifact,
                }
            )
        return plans

    async def _authorize_effects(
        self,
        job: Job,
        epoch: int,
        offset: int,
        effects: Sequence[dict[str, Any]],
        sources: Sequence[dict[str, Any]],
        effect_stage: str | None = None,
    ) -> EffectFence:
        if not effects:
            return EffectFence.AUTHORIZED
        result: EffectFence = await self._io(
            self.store.authorize_effects,
            job_id=job.job_id,
            epoch=epoch,
            scope=job.scope,
            stage=effect_stage or job.stage,
            offset=offset,
            effects=effects,
            sources=sources,
            at=self.clock(),
        )
        return result

    async def _finalize_effects(
        self,
        job: Job,
        epoch: int,
        effects: Sequence[dict[str, Any]],
        failure: dict[str, Any] | None = None,
    ) -> EffectFence:
        if not effects:
            return EffectFence.OK
        result: EffectFence = await self._io(
            self.store.finalize_effects,
            job_id=job.job_id,
            epoch=epoch,
            effects=effects,
            failure=failure,
            at=self.clock(),
        )
        return result

    async def _compensate(
        self,
        handler: StageHandler,
        job: Job,
        offset: int,
        prepared: Any,
        effects: Sequence[dict[str, Any]],
    ) -> None:
        """Remove a projection the fence refused after the fact (a source erased mid-commit)."""
        tap.event(
            "runtime.outbox.compensate",
            job_id=job.job_id,
            stage=job.stage,
            offset=offset,
            effects=len(effects),
            handled=handler.compensate is not None,
        )
        if handler.compensate is None:
            _log.warning(
                "outbox.uncompensated_projection",
                job_id=job.job_id,
                stage=job.stage,
                offset=offset,
                layers=sorted({str(e.get("layer")) for e in effects}),
            )
            return
        compensate = handler.compensate
        if handler.commit_locked and self._commit_lock is not None:
            async with self._commit_lock(job.scope.owner):
                await compensate(job.scope, offset, prepared, effects)
        else:
            await compensate(job.scope, offset, prepared, effects)

    async def _record_failure(
        self,
        handler: StageHandler,
        job: Job,
        epoch: int,
        offset: int,
        sources: Sequence[dict[str, Any]],
        source_ids: Sequence[str],
        effect_stage: str | None,
        exc: BaseException,
    ) -> None:
        """Persist a failed prepare as failed effects. Never hides ``exc``: a fence met here is
        raised chained to it, and an item whose effects were already acknowledged (``COMPLETE``)
        or whose replay diverged keeps ``exc`` as the outcome without re-finalizing."""
        failed_effects = self._effect_plans(handler, job, offset, None, sources, effect_stage)
        door = await self._authorize_effects(
            job, epoch, offset, failed_effects, sources, effect_stage
        )
        if door is EffectFence.COMPLETE or door is EffectFence.REPLAY_DIVERGED:
            return
        if door is not EffectFence.AUTHORIZED:
            raise _fence_for(door) from exc
        done = await self._finalize_effects(
            job, epoch, failed_effects, self._failure(job, effect_stage, exc, source_ids)
        )
        if done in (EffectFence.STALE_LEASE, EffectFence.STALE_SOURCE, EffectFence.BUSY):
            raise _fence_for(done) from exc

    @staticmethod
    def _failure(
        job: Job, effect_stage: str | None, exc: BaseException, source_ids: Sequence[str]
    ) -> dict[str, Any]:
        return {
            "stage": effect_stage or job.stage,
            "error_class": type(exc).__name__,
            "attempts": job.attempts,
            "retry_eligible": True,
            "source_ids": list(source_ids),
        }

    async def _commit(
        self, handler: StageHandler, scope: OwnerScope, offset: int, prepared: Any
    ) -> None:
        if handler.commit_locked and self._commit_lock is not None:
            async with self._commit_lock(scope.owner):
                await handler.commit(scope, offset, prepared)
        else:
            await handler.commit(scope, offset, prepared)

    async def _execute_item(
        self,
        handler: StageHandler,
        job: Job,
        epoch: int,
        offset: int,
        *,
        source_stage: str | None = None,
        effect_stage: str | None = None,
    ) -> Any:
        """Prepare, authorize durable effects, project, then acknowledge completion."""
        stage = source_stage or job.stage
        if insp.inspect_enabled():
            with tap.request("job", stage=f"outbox_{stage}", offset=offset):
                return await self._execute_item_inner(
                    handler,
                    job,
                    epoch,
                    offset,
                    source_stage=source_stage,
                    effect_stage=effect_stage,
                )
        return await self._execute_item_inner(
            handler,
            job,
            epoch,
            offset,
            source_stage=source_stage,
            effect_stage=effect_stage,
        )

    async def _execute_item_inner(
        self,
        handler: StageHandler,
        job: Job,
        epoch: int,
        offset: int,
        *,
        source_stage: str | None = None,
        effect_stage: str | None = None,
    ) -> Any:
        stage = source_stage or job.stage
        payload = self.store.get_item(job.scope, stage, offset)
        if payload is None or payload.get(CANCELLED_KEY):  # RM-142: superseded by activation
            return None
        source_ids = tuple(str(value) for value in payload.get("record_ids", ()) if value)
        sources: list[dict[str, Any]] = []
        if handler.effect_results is not None and source_ids:
            sources = await self._io(self.store.source_snapshots, job.scope, stage, source_ids)
            if len(sources) != len(dict.fromkeys(source_ids)):
                erased = await self._io(self.store.erased_sources, job.scope, stage, source_ids)
                if erased:  # the item still names an erased source: it can never be authorized
                    raise _DeterministicFence(
                        "source_missing", f"{len(erased)} input source(s) erased"
                    )
                raise _Fenced("source")  # not accepted yet: a later drain re-reads it
        try:
            if self._backpressure is None and self._ledger is None:
                prepared = await self._phases(handler, job.scope, offset, payload, stage)
            else:
                with priority_scope(Priority.DERIVE):
                    prepared = await self._phases(handler, job.scope, offset, payload, stage)
                if self._ledger is not None:
                    queued_at = self.store.item_created_at(job.scope, stage, offset)
                    if queued_at is not None:
                        self._ledger.record_lag(
                            max(0.0, (self.clock() - queued_at).total_seconds())
                        )
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError) and handler.effect_results is not None:
                await self._record_failure(
                    handler, job, epoch, offset, sources, source_ids, effect_stage, exc
                )
            raise
        effects = self._effect_plans(handler, job, offset, prepared, sources, effect_stage)
        authorization = await self._authorize_effects(
            job, epoch, offset, effects, sources, effect_stage
        )
        if authorization is EffectFence.COMPLETE:
            return prepared
        if authorization is not EffectFence.AUTHORIZED:
            raise _fence_for(authorization)  # nothing projected yet: nothing to compensate
        try:
            if insp.inspect_enabled():
                with tap.span("runtime.outbox.commit_item", stage=stage, offset=offset):
                    await self._commit(handler, job.scope, offset, prepared)
            else:
                await self._commit(handler, job.scope, offset, prepared)
        except BaseException as exc:
            if not isinstance(exc, asyncio.CancelledError):
                done = await self._finalize_effects(
                    job, epoch, effects, self._failure(job, effect_stage, exc, source_ids)
                )
                if done is EffectFence.STALE_SOURCE:  # a partial projection of erased content
                    await self._compensate(handler, job, offset, prepared, effects)
                if done in (EffectFence.STALE_LEASE, EffectFence.STALE_SOURCE):
                    raise _fence_for(done) from exc
            raise
        done = await self._finalize_effects(job, epoch, effects)
        if done is EffectFence.STALE_SOURCE:
            await self._compensate(handler, job, offset, prepared, effects)
        if done is not EffectFence.OK:
            raise _fence_for(done)
        return prepared

    async def _run_claim(
        self,
        claim: Claim,
        report: DrainReport,
        *,
        limit: int | None = None,
        on_offset: Callable[[int], None] | None = None,
    ) -> bool:
        """Walk the claimed range, checkpointing each offset, then commit the job. With ``limit``
        (RM-132: the Temporal Activity's bounded batch) at most ``limit`` offsets run; when the
        range is longer the job is NOT committed and ``False`` is returned, so the caller hands
        it back (``release``) and the next claim resumes at the watermark. ``on_offset`` is told
        each offset before it runs (an Activity heartbeat). Inline drains pass neither."""
        job, epoch = claim.job, claim.lease.epoch
        handler = self._handlers[job.stage]
        start = self.book.progress(job.scope, job.stage).watermark + 1
        for offset in range(start, job.target_offset + 1):
            if limit is not None and offset - start >= limit:
                return False
            if self._stopping:
                raise _Stop
            if on_offset is not None:
                on_offset(offset)
            if not (await self._io(self.book.heartbeat, job.job_id, epoch, self.lease_ttl)).ok:
                raise _Fenced
            failed: dict[int, tuple[str, str | None]] = {}
            try:
                await self._execute_item(handler, job, epoch, offset)
            except (asyncio.CancelledError, _Fenced):
                raise  # a transient fence is not a hole: no checkpoint moves past this offset
            except _DeterministicFence as fence:
                failed[offset] = (fence.reason, fence.detail)
                report.holes += 1
                report.hole_fences[fence.reason] = report.hole_fences.get(fence.reason, 0) + 1
                self._alert_hole(job.job_id, job.scope, job.stage, offset, fence)
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
        await self._prune_finished(job.scope, job.stage)
        tap.event(
            "runtime.outbox.commit",
            job_id=job.job_id,
            stage=job.stage,
            epoch=epoch,
            through=job.target_offset,
        )
        return True

    async def _retry_holes(self, scope: OwnerScope | None, stage: str, report: DrainReport) -> None:
        """Retry holes under a fresh durable lease, never as an unfenced callback."""
        handler = self._handlers[stage]
        for sc in self._scopes_with_holes(scope, stage):
            for hole in self.book.holes_to_retry(sc, stage):
                if self._stopping:
                    return
                report.hole_retries += 1
                retry_stage = f"{stage}.hole.{hole.offset}"
                await self._io(self.book.enqueue, sc, retry_stage, 0)
                claim = await self._io(
                    self.book.claim,
                    self.owner,
                    self.lease_ttl,
                    scope=sc,
                    stage=retry_stage,
                )
                if claim is None:
                    continue
                try:
                    if not (
                        await self._io(
                            self.book.heartbeat,
                            claim.job.job_id,
                            claim.lease.epoch,
                            self.lease_ttl,
                        )
                    ).ok:
                        raise _Fenced
                    await self._execute_item(
                        handler,
                        claim.job,
                        claim.lease.epoch,
                        hole.offset,
                        source_stage=stage,
                        effect_stage=stage,
                    )
                except asyncio.CancelledError:
                    raise
                except _DeterministicFence as fence:
                    await self._io(
                        self.book.fail,
                        claim.job.job_id,
                        claim.lease.epoch,
                        f"fenced: {fence.reason}",
                        "raised",
                        retryable=False,
                    )
                    await self._io(
                        self.book.record_hole_result,
                        sc,
                        stage,
                        hole.offset,
                        False,
                        fence.detail,
                        reason=fence.reason,
                    )
                    self._alert_hole(claim.job.job_id, sc, stage, hole.offset, fence)
                    report.hole_fences[fence.reason] = report.hole_fences.get(fence.reason, 0) + 1
                except _Fenced as fence:
                    if fence.reason in ("lease", "busy"):
                        # The retry job is no longer ours: spend nothing, record nothing.
                        report.fenced += 1
                        await self._release(claim)
                        continue
                    await self._io(
                        self.book.fail,
                        claim.job.job_id,
                        claim.lease.epoch,
                        f"fenced: {fence.reason}",
                        "raised",
                        retryable=False,
                    )
                    await self._io(
                        self.book.record_hole_result,
                        sc,
                        stage,
                        hole.offset,
                        False,
                        f"fenced: {fence.reason}",
                    )
                except Exception as exc:
                    await self._io(
                        self.book.fail,
                        claim.job.job_id,
                        claim.lease.epoch,
                        f"{type(exc).__name__}: {exc}"[:300],
                        "raised",
                        retryable=False,
                    )
                    await self._io(
                        self.book.record_hole_result,
                        sc,
                        stage,
                        hole.offset,
                        False,
                        f"{type(exc).__name__}: {exc}"[:300],
                    )
                else:
                    checkpoint = await self._io(
                        self.book.checkpoint, claim.job.job_id, claim.lease.epoch, 0
                    )
                    if not checkpoint.ok:
                        raise _Fenced
                    committed = await self._io(
                        self.book.commit, claim.job.job_id, claim.lease.epoch
                    )
                    if not committed.ok:
                        raise _Fenced
                    await self._io(self.book.record_hole_result, sc, stage, hole.offset, True)
                    await self._prune_finished(sc, retry_stage)
                    report.hole_filled += 1

    @staticmethod
    def _alert_hole(
        job_id: str, scope: OwnerScope, stage: str, offset: int, fence: _DeterministicFence
    ) -> None:
        """A deterministic fence became a hole: a structured alert an operator acts on (waive the
        hole, or fix and requeue the item). Identifiers only, never record ids or content."""
        tap.event(
            "runtime.outbox.hole",
            job_id=job_id,
            stage=stage,
            offset=offset,
            reason=fence.reason,
            error=type(fence).__name__,
        )
        _log.error(
            "outbox.deterministic_hole",
            alert=True,
            job_id=job_id,
            tenant=scope.tenant,
            owner_kind=scope.owner_kind,
            stage=stage,
            offset=offset,
            reason=fence.reason,
            detail=fence.detail,
            waivable=True,
        )

    def _scopes_with_holes(self, scope: OwnerScope | None, stage: str) -> list[OwnerScope]:
        if scope is not None:
            return [scope]
        return self.store.hole_scopes(stage)  # RM-159: the progress rows, not every job

    async def _prune_finished(self, scope: OwnerScope, stage: str) -> None:
        """RM-159: drop the finished job rows of ``(scope, stage)`` that nothing needs any more
        (bounded growth); see ``SqlJobStore.prune_finished`` for what is kept."""
        await self._io(self.store.prune_finished, scope, stage, OUTBOX_FINISHED_JOBS_KEPT)

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
        drain runs, the caller drains inline within a monotonic asyncio deadline. The injected
        clock still controls barrier/lease semantics, but a frozen clock cannot make a slow
        prepare/commit exceed the wait budget indefinitely. A non-positive timeout only reads
        readiness and never starts a drain.

        Expiry cancels this caller's inline drain through its normal claim-release path; items
        remain durable for a later drain. An independently running drain is not cancelled. Caller
        cancellation propagates unchanged. Handler effects retain the outbox's at-least-once
        semantics and must remain idempotent if interrupted before checkpointing. Like every
        asyncio deadline this is cooperative: blocking I/O or cancellation cleanup may delay
        return."""
        limit = max(
            timedelta(0), timeout if isinstance(timeout, timedelta) else timedelta(seconds=timeout)
        )
        started = self.clock()
        targets = {c: self.store.last_offset(scope, c) for c in capabilities}
        tap.event(
            "runtime.barrier.wait",
            phase="begin",
            capabilities=sorted(capabilities),
            timeout_s=limit.total_seconds(),
        )
        deadline = asyncio.timeout(max(0.0, limit.total_seconds()))
        try:
            async with deadline:
                while True:
                    outcome, waiting = self._barrier(scope, targets, limit, started)
                    if waiting and not self._drain_lock.locked() and not self._stopping:
                        await self.drain(scope)
                        outcome, waiting = self._barrier(scope, targets, limit, started)
                    if not waiting:
                        break
                    await self._sleep(self._poll_s)
        except TimeoutError:
            if not deadline.expired():
                raise
            # The injected clock may not have advanced. A zero-wait snapshot reports any
            # remaining work as partial rather than restarting a wait on that frozen clock.
            outcome, _ = self._barrier(scope, targets, timedelta(0), self.clock())
        tap.event(
            "runtime.barrier.wait",
            phase="end",
            status=outcome.status,
            missing=list(outcome.missing),
            holes=sum(len(v) for v in outcome.holes.values()),
        )
        return outcome

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
