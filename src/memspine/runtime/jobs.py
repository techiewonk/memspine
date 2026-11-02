"""Background job bookkeeping (LIB-07): leases with epochs, progress, coalescing, triggers.

Pure library, not wired (wiring is F-04 / RT-01 / RT-02 / O4-01a). Standard library, pydantic and
``memspine.schemas`` only. No I/O, no real clock (a ``Clock`` is injected), no threads.

Binding decisions (schema session 8, ER-07/ER-09/ER-15/ER-26, OF-1/OF-2):

* A claim takes a lease whose ``epoch`` strictly increases per job; every later operation (heartbeat,
  checkpoint, commit, fail, release) is conditional on the epoch still being current and the job still
  running. A stale worker is refused and the refusal is logged (``JobStore.log``).
* Progress is a contiguous watermark plus explicit holes ``(offset, reason, attempts, last_error)``.
  Holes stay visible until filled or waived; the watermark never passes an offset silently.
* A job's snapshot names its target offset. The target may only be raised while the job is queued and
  has never been claimed; the first claim freezes it. Triggers that arrive while a job is active
  coalesce into at most ONE follow-up job.
* Unfinished jobs persist: all state lives in the store, so a new ``JobBook`` over the same store
  resumes after a stop. ``drain_plan`` decides which running jobs may finish before a deadline.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, runtime_checkable

from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.jobs import (
    FAILURE_CLASSES,
    NON_RETRYABLE_HOLE_REASONS,
    Hole,
    JobLease,
    Progress,
)
from memspine.schemas.public import CostRecord, JobHandle
from memspine.schemas.public.receipt import JobState

__all__ = [
    "Claim",
    "Clock",
    "DrainPlan",
    "FenceEvent",
    "FenceResult",
    "IllegalTransition",
    "InMemoryJobStore",
    "Job",
    "JobBook",
    "JobStore",
    "ManualClock",
    "PurposeLimit",
    "RecoveryBudget",
    "RetryBudget",
    "ScopeKey",
    "TriggerConfig",
    "TriggerDecision",
    "TriggerStats",
    "Triggers",
    "maintenance_due",
    "scope_key",
    "should_run",
]

Clock = Callable[[], datetime]
ScopeKey = tuple[str, str, str]
#: A custom picker claims its chosen job atomically; a claimer that lost that job picks again.
_CLAIM_RACE_ATTEMPTS = 3

TRANSITIONS: Mapping[str, frozenset[str]] = {
    "queued": frozenset({"running", "cancelled"}),
    "running": frozenset({"succeeded", "failed", "cancelled", "queued"}),
    "succeeded": frozenset(),
    "failed": frozenset(),
    "cancelled": frozenset(),
    "coalesced": frozenset(),
}
TERMINAL: frozenset[str] = frozenset({"succeeded", "failed", "cancelled", "coalesced"})
LIVE: frozenset[str] = frozenset({"queued", "running"})


class IllegalTransition(ValueError):
    """A job state change that the handle state machine does not allow."""


def scope_key(scope: OwnerScope) -> ScopeKey:
    return (scope.tenant, scope.owner, scope.owner_kind)


class ManualClock:
    """Deterministic clock for tests and for callers that drive time themselves."""

    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0.0, **kw: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds, **kw)
        return self._now

    def set(self, now: datetime) -> None:
        self._now = now


@dataclass(frozen=True)
class RetryBudget:
    """ER-15: attempts per job and per hole, with exponential backoff."""

    max_attempts: int = 3
    max_hole_attempts: int = 3
    base_backoff: timedelta = timedelta(seconds=30)
    max_backoff: timedelta = timedelta(minutes=30)

    def backoff(self, attempts: int) -> timedelta:
        return min(self.base_backoff * (2 ** max(attempts - 1, 0)), self.max_backoff)  # type: ignore[no-any-return]  # CI-GREEN: pre-existing typing debt


@dataclass(frozen=True)
class Job:
    """One job record. Immutable; every change goes through ``dataclasses.replace`` in the book."""

    job_id: str
    seq: int
    scope: OwnerScope
    stage: str
    target_offset: int
    state: JobState
    created_at: datetime
    payload: str | None = None  # opaque reference to the input snapshot (hash / blob id)
    frozen: bool = False  # True from the first claim: the snapshot target is then immutable
    epoch: int = 0  # 0 = never claimed
    lease_owner: str | None = None
    lease_acquired_at: datetime | None = None
    lease_heartbeat_at: datetime | None = None
    lease_until: datetime | None = None
    attempts: int = 0
    not_before: datetime | None = None
    error: str | None = None
    failure_class: str | None = None
    coalesced_into: str | None = None

    @property
    def key(self) -> tuple[ScopeKey, str]:
        return (scope_key(self.scope), self.stage)

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL

    def handle(self) -> JobHandle:
        return JobHandle(
            job_id=self.job_id,
            kind=self.stage,
            state=self.state,
            scope=self.scope,
            created_at=self.created_at,
            target_offset=self.target_offset,
            coalesced_into=self.coalesced_into,
            error=self.error if self.state == "failed" else None,
        )


@dataclass(frozen=True)
class FenceEvent:
    """A refused operation (stale epoch or wrong state); the evidence that fencing worked."""

    at: datetime
    job_id: str
    op: str
    presented_epoch: int
    current_epoch: int | None
    state: str | None
    reason: str


@dataclass(frozen=True)
class FenceResult:
    ok: bool
    reason: str | None = None
    job: Job | None = None


@dataclass(frozen=True)
class Claim:
    job: Job
    lease: JobLease


@dataclass(frozen=True)
class DrainPlan:
    """Bounded drain on stop: ``finish`` may complete before ``deadline``; ``release`` are running jobs
    that cannot and go back to queued; ``keep_queued`` stay queued. Nothing is dropped."""

    deadline: datetime
    finish: tuple[str, ...]
    release: tuple[str, ...]
    keep_queued: tuple[str, ...]


ExcludeKeys = Collection[tuple[ScopeKey, str]]


@runtime_checkable
class JobStore(Protocol):
    """Persistence behind the book. A real store makes each call atomic.

    RM-159: the book never lists every job on a hot path. ``live_jobs``, ``expired_leases`` and
    ``claim_job`` return only the rows they concern (an SQL store answers them from indexes),
    and ``claim_job`` is one atomic step: it leases the oldest ready job (FIFO by ``seq``; or the
    named ``job_id``, for a custom picker), bumps its epoch, freezes it and records the snapshot
    target in the progress row. ``jobs`` stays for inspection only."""

    def next_seq(self) -> int: ...
    def put_job(self, job: Job) -> None: ...
    def put_job_if(self, job: Job, *, state: str, epoch: int) -> bool: ...
    def get_job(self, job_id: str) -> Job | None: ...
    def jobs(self) -> list[Job]: ...  # ordered by seq; inspection, never a hot path
    def live_jobs(self, scope: ScopeKey | None = None, stage: str | None = None) -> list[Job]: ...
    def expired_leases(self, now: datetime) -> list[Job]: ...
    def claimable(
        self,
        now: datetime,
        *,
        scope: ScopeKey | None = None,
        stage: str | None = None,
        exclude: ExcludeKeys = (),
    ) -> list[Job]: ...
    def claim_job(
        self,
        owner: str,
        now: datetime,
        until: datetime,
        *,
        scope: ScopeKey | None = None,
        stage: str | None = None,
        exclude: ExcludeKeys = (),
        job_id: str | None = None,
    ) -> Job | None: ...
    def get_progress(self, key: tuple[ScopeKey, str]) -> Progress | None: ...
    def put_progress(self, progress: Progress) -> None: ...
    def append_log(self, event: FenceEvent) -> None: ...
    def log(self) -> list[FenceEvent]: ...


def ready_candidates(
    live: Sequence[Job],
    now: datetime,
    *,
    scope: ScopeKey | None = None,
    stage: str | None = None,
    exclude: ExcludeKeys = (),
) -> list[Job]:
    """The claim rule over live jobs (seq order): per (scope, stage) key with no running job and
    not excluded, the oldest queued job whose backoff ended. The FIFO claim takes the one with
    the smallest ``seq``; an SQL store states the same rule as one query."""
    running = {j.key for j in live if j.state == "running"}
    by_key: dict[tuple[ScopeKey, str], Job] = {}
    for j in sorted(live, key=lambda j: j.seq):  # the frozen retry precedes its follow-up
        if j.state != "queued" or j.key in running or j.key in by_key or j.key in exclude:
            continue
        if scope is not None and scope_key(j.scope) != scope:
            continue
        if stage is not None and j.stage != stage:
            continue
        if j.not_before is not None and j.not_before > now:
            continue
        by_key[j.key] = j
    return list(by_key.values())


class InMemoryJobStore:
    """Reference store. Survives a simulated stop because the book keeps no state of its own."""

    def __init__(self) -> None:
        self._seq = 0
        self._jobs: dict[str, Job] = {}
        self._progress: dict[tuple[ScopeKey, str], Progress] = {}
        self._log: list[FenceEvent] = []

    def next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def put_job(self, job: Job) -> None:
        self._jobs[job.job_id] = job

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def put_job_if(self, job: Job, *, state: str, epoch: int) -> bool:
        current = self._jobs.get(job.job_id)
        if current is None or current.state != state or current.epoch != epoch:
            return False
        self._jobs[job.job_id] = job
        return True

    def jobs(self) -> list[Job]:
        return sorted(self._jobs.values(), key=lambda j: j.seq)

    def live_jobs(self, scope: ScopeKey | None = None, stage: str | None = None) -> list[Job]:
        return [
            j
            for j in self.jobs()
            if j.state in LIVE
            and (scope is None or scope_key(j.scope) == scope)
            and (stage is None or j.stage == stage)
        ]

    def expired_leases(self, now: datetime) -> list[Job]:
        return [
            j
            for j in self.live_jobs()
            if j.state == "running" and j.lease_until is not None and j.lease_until <= now
        ]

    def claimable(
        self,
        now: datetime,
        *,
        scope: ScopeKey | None = None,
        stage: str | None = None,
        exclude: ExcludeKeys = (),
    ) -> list[Job]:
        return ready_candidates(
            self.live_jobs(scope, stage), now, scope=scope, stage=stage, exclude=exclude
        )

    def claim_job(
        self,
        owner: str,
        now: datetime,
        until: datetime,
        *,
        scope: ScopeKey | None = None,
        stage: str | None = None,
        exclude: ExcludeKeys = (),
        job_id: str | None = None,
    ) -> Job | None:
        ready = self.claimable(now, scope=scope, stage=stage, exclude=exclude)
        if job_id is not None:
            ready = [j for j in ready if j.job_id == job_id]
        if not ready:
            return None
        chosen = min(ready, key=lambda j: j.seq)
        job = replace(
            chosen,
            state="running",
            frozen=True,
            epoch=chosen.epoch + 1,
            lease_owner=owner,
            lease_acquired_at=now,
            lease_heartbeat_at=now,
            lease_until=until,
            not_before=None,
        )
        self._jobs[job.job_id] = job
        prog = self._progress.get(job.key) or Progress(
            scope=job.scope, job_kind=job.stage, watermark=-1
        )
        target = max(job.target_offset, prog.watermark)
        self._progress[job.key] = prog.model_copy(update={"target_offset": target})
        return job

    def get_progress(self, key: tuple[ScopeKey, str]) -> Progress | None:
        return self._progress.get(key)

    def put_progress(self, progress: Progress) -> None:
        self._progress[(scope_key(progress.scope), progress.job_kind)] = progress

    def append_log(self, event: FenceEvent) -> None:
        self._log.append(event)

    def log(self) -> list[FenceEvent]:
        return list(self._log)


@runtime_checkable
class Picker(Protocol):
    def pick(self, candidates: Sequence[Job]) -> Job: ...


class _Fifo:
    def pick(self, candidates: Sequence[Job]) -> Job:
        return min(candidates, key=lambda j: j.seq)


@dataclass(frozen=True)
class PurposeLimit:
    calls: int | None = None
    tokens: int | None = None


class RecoveryBudget:
    """Recovery budget per source in calls and tokens per purpose (session 9). Spending is
    all-or-nothing; a refused spend means the caller records a ``budget_exhausted`` hole."""

    METERED = ("extraction", "recovery", "coverage")

    def __init__(self, limits: Mapping[str, PurposeLimit]) -> None:
        for p in limits:
            if p not in self.METERED:
                raise ValueError(f"unknown purpose {p!r}")
        self._limits = dict(limits)
        self._spent: dict[tuple[str, str], tuple[int, int]] = {}

    def spent(self, source: str, purpose: str) -> tuple[int, int]:
        return self._spent.get((source, purpose), (0, 0))

    def remaining(self, source: str, purpose: str) -> tuple[int | None, int | None]:
        lim = self._limits.get(purpose, PurposeLimit())
        c, t = self.spent(source, purpose)
        return (
            None if lim.calls is None else max(lim.calls - c, 0),
            None if lim.tokens is None else max(lim.tokens - t, 0),
        )

    def try_spend(self, source: str, purpose: str, calls: int = 1, tokens: int = 0) -> bool:
        if calls < 0 or tokens < 0:
            raise ValueError("negative spend")
        if purpose not in self.METERED:
            return True  # unmetered purposes (read, other)
        rc, rt = self.remaining(source, purpose)
        if (rc is not None and calls > rc) or (rt is not None and tokens > rt):
            return False
        c, t = self.spent(source, purpose)
        self._spent[(source, purpose)] = (c + calls, t + tokens)
        return True

    def charge(self, source: str, record: CostRecord) -> bool:
        return self.try_spend(
            source, record.purpose, record.calls, record.tokens_in + record.tokens_out
        )

    def exhausted(self, source: str, purpose: str) -> bool:
        rc, rt = self.remaining(source, purpose)
        return rc == 0 or rt == 0


class JobBook:
    """All job operations over a ``JobStore``. Stateless apart from the store, so it can be rebuilt
    after a stop."""

    def __init__(
        self,
        store: JobStore,
        clock: Clock,
        budget: RetryBudget | None = None,
        picker: Picker | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.budget = budget or RetryBudget()
        self.picker: Picker = picker or _Fifo()

    # ------------------------------------------------------------------ helpers
    def _put(self, job: Job, new_state: str | None = None, **changes: Any) -> Job:
        if new_state is not None:
            if new_state not in TRANSITIONS[job.state]:
                raise IllegalTransition(f"{job.job_id}: {job.state} -> {new_state}")
            changes["state"] = new_state
        out = replace(job, **changes)
        self.store.put_job(out)
        return out

    def _refuse(
        self, job_id: str, op: str, epoch: int, job: Job | None, reason: str
    ) -> FenceResult:
        self.store.append_log(
            FenceEvent(
                at=self.clock(),
                job_id=job_id,
                op=op,
                presented_epoch=epoch,
                current_epoch=None if job is None else job.epoch,
                state=None if job is None else job.state,
                reason=reason,
            )
        )
        return FenceResult(False, reason, job)

    def _fence(self, job_id: str, op: str, epoch: int) -> tuple[Job | None, FenceResult | None]:
        job = self.store.get_job(job_id)
        if job is None:
            return None, self._refuse(job_id, op, epoch, None, "unknown job")
        if job.state != "running":
            return job, self._refuse(job_id, op, epoch, job, f"job is {job.state}")
        if epoch != job.epoch:
            return job, self._refuse(job_id, op, epoch, job, "stale epoch")
        return job, None

    def get(self, job_id: str) -> Job:
        job = self.store.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def handle(self, job_id: str) -> JobHandle:
        return self.get(job_id).handle()

    def progress(self, scope: OwnerScope, stage: str) -> Progress:
        p = self.store.get_progress((scope_key(scope), stage))
        return p if p is not None else Progress(scope=scope, job_kind=stage, watermark=-1)

    def live_jobs(self, scope: OwnerScope | None = None, stage: str | None = None) -> list[Job]:
        return self.store.live_jobs(None if scope is None else scope_key(scope), stage)

    def queue_depth(self, scope: OwnerScope | None = None) -> int:
        """Accepted, unfinished work (queued + running); the number QuotaPolicy compares to its cap."""
        return len(self.live_jobs(scope))

    # ------------------------------------------------------------------ enqueue / coalesce
    def enqueue(
        self, scope: OwnerScope, stage: str, target_offset: int, payload: str | None = None
    ) -> JobHandle:
        """Accept a trigger. Returns a ``queued`` handle for a new job, or a ``coalesced`` handle
        pointing at the job that covers it. At most one unfrozen follow-up exists per (scope, stage)."""
        now = self.clock()
        live = self.live_jobs(scope, stage)
        follow_up = next((j for j in live if j.state == "queued" and not j.frozen), None)
        if follow_up is not None:
            if target_offset > follow_up.target_offset:
                follow_up = self._put(follow_up, target_offset=target_offset)
            return self._coalesced(scope, stage, follow_up, now)
        active = [j for j in live if j.frozen]
        if active:
            cover = max(active, key=lambda j: j.target_offset)
            if cover.target_offset >= target_offset:
                return self._coalesced(scope, stage, cover, now)
        seq = self.store.next_seq()
        job = Job(
            job_id=f"job-{seq}",
            seq=seq,
            scope=scope,
            stage=stage,
            target_offset=target_offset,
            state="queued",
            created_at=now,
            payload=payload,
        )
        self.store.put_job(job)
        return job.handle()

    def _coalesced(self, scope: OwnerScope, stage: str, into: Job, now: datetime) -> JobHandle:
        seq = self.store.next_seq()
        rec = Job(
            job_id=f"job-{seq}",
            seq=seq,
            scope=scope,
            stage=stage,
            target_offset=into.target_offset,
            state="coalesced",
            created_at=now,
            coalesced_into=into.job_id,
        )
        self.store.put_job(rec)
        return rec.handle()

    # ------------------------------------------------------------------ claim / lease
    def claim(
        self,
        owner: str,
        ttl: timedelta,
        *,
        scope: OwnerScope | None = None,
        stage: str | None = None,
        exclude: Collection[tuple[ScopeKey, str]] = (),
    ) -> Claim | None:
        """Lease the next ready job. One running job per (scope, stage). Expired leases are reclaimed
        first. The first claim freezes the snapshot target. ``exclude`` names ``(scope, stage)``
        keys this caller already met a transient fence on, so one drain moves on to other scopes.

        The lease is one atomic store step (``JobStore.claim_job``, RM-159): two claimers never
        both win a job, and never hold two jobs of one key. With a custom picker the book picks
        among ``claimable`` and claims that job atomically; a lost race picks again."""
        if ttl <= timedelta(0):
            raise ValueError("ttl must be positive")
        self.expire_leases()
        now = self.clock()
        until = now + ttl
        k = None if scope is None else scope_key(scope)
        job: Job | None = None
        if isinstance(self.picker, _Fifo):
            job = self.store.claim_job(owner, now, until, scope=k, stage=stage, exclude=exclude)
        else:
            for _ in range(_CLAIM_RACE_ATTEMPTS):
                ready = self.store.claimable(now, scope=k, stage=stage, exclude=exclude)
                if not ready:
                    return None
                chosen = self.picker.pick(ready)
                job = self.store.claim_job(
                    owner, now, until, scope=k, stage=stage, exclude=exclude, job_id=chosen.job_id
                )
                if job is not None:
                    break
        if job is None:
            return None
        lease = JobLease(
            scope=job.scope,
            job_id=job.job_id,
            owner=owner,
            epoch=job.epoch,
            acquired_at=now,
            heartbeat_at=now,
            lease_until=until,
        )
        return Claim(job, lease)

    def heartbeat(self, job_id: str, epoch: int, ttl: timedelta) -> FenceResult:
        job, refused = self._fence(job_id, "heartbeat", epoch)
        if refused is not None or job is None:
            return refused or FenceResult(False, "unknown job")
        now = self.clock()
        job = self._put(job, lease_heartbeat_at=now, lease_until=now + ttl)
        return FenceResult(True, None, job)

    renew = heartbeat

    def expire_leases(self) -> list[str]:
        """Make running jobs whose lease ran out reclaimable. A lost lease counts as an attempt; an
        exhausted job fails terminally and its range becomes holes.

        Each transition is conditional on the state and epoch read (``put_job_if``), so a
        process that expired a lease and re-claimed the job is never overwritten by a second
        process expiring the same, already stale, row."""
        now = self.clock()
        out: list[str] = []
        for j in self.store.expired_leases(now):
            done = self._retry_or_fail(
                j, "lease expired", "raised", retryable=True, backoff=False, expect=True
            )
            if done is not None:
                out.append(j.job_id)
        return out

    # ------------------------------------------------------------------ progress
    def _range(self, job: Job) -> range:
        return range(self.progress(job.scope, job.stage).watermark + 1, job.target_offset + 1)

    def _advance(
        self,
        job: Job,
        through: int,
        failed: Mapping[int, tuple[str, str | None]],
        *,
        finish: bool,
    ) -> Progress:
        prog = self.progress(job.scope, job.stage)
        if through < prog.watermark:
            raise ValueError("watermark cannot move backwards")
        if through > job.target_offset and through > prog.watermark:
            raise ValueError("cannot pass the snapshot target offset")
        bad = [o for o in failed if not prog.watermark < o <= through]
        if bad:
            raise ValueError(f"failed offsets outside the advanced range: {bad}")
        holes = {h.offset: h for h in prog.holes}
        for off, (reason, err) in failed.items():
            if reason not in FAILURE_CLASSES:
                raise ValueError(f"unknown failure class {reason!r}")
            holes[off] = Hole(offset=off, reason=reason, attempts=1, last_error=err)  # type: ignore[arg-type]
        new = Progress(
            scope=job.scope,
            job_kind=job.stage,
            watermark=through,
            target_offset=None if finish else job.target_offset,
            holes=[holes[o] for o in sorted(holes)],
        )
        self.store.put_progress(new)
        return new

    def checkpoint(
        self,
        job_id: str,
        epoch: int,
        through: int,
        failed: Mapping[int, tuple[str, str | None]] | None = None,
    ) -> FenceResult:
        """Fenced watermark advance while running. Every offset in (watermark, through] is handled:
        listed in ``failed`` (becomes a hole) or successful. Nothing is skipped silently."""
        job, refused = self._fence(job_id, "checkpoint", epoch)
        if refused is not None or job is None:
            return refused or FenceResult(False, "unknown job")
        self._advance(job, through, failed or {}, finish=False)
        return FenceResult(True, None, job)

    def commit(
        self,
        job_id: str,
        epoch: int,
        failed: Mapping[int, tuple[str, str | None]] | None = None,
    ) -> FenceResult:
        """Finish: advance the watermark to the snapshot target and mark the job succeeded. Refused
        (and logged) unless the epoch is current."""
        job, refused = self._fence(job_id, "commit", epoch)
        if refused is not None or job is None:
            return refused or FenceResult(False, "unknown job")
        prog = self.progress(job.scope, job.stage)
        self._advance(job, max(job.target_offset, prog.watermark), failed or {}, finish=True)
        job = self._put(job, "succeeded", lease_owner=None, lease_until=None, error=None)
        return FenceResult(True, None, job)

    def fail(
        self,
        job_id: str,
        epoch: int,
        error: str,
        failure_class: str = "raised",
        *,
        retryable: bool = True,
    ) -> FenceResult:
        """Record a failure: retry within the budget (backoff via ``not_before``), otherwise fail the
        job terminally and turn the unhandled range into holes (watermark passes them, visibly)."""
        job, refused = self._fence(job_id, "fail", epoch)
        if refused is not None or job is None:
            return refused or FenceResult(False, "unknown job")
        return FenceResult(True, None, self._retry_or_fail(job, error, failure_class, retryable))

    def _put_if(self, job: Job, new_state: JobState, **changes: Any) -> Job | None:
        """``_put`` conditional on ``job`` still being the stored state and epoch."""
        if new_state not in TRANSITIONS[job.state]:
            raise IllegalTransition(f"{job.job_id}: {job.state} -> {new_state}")
        out = replace(job, state=new_state, **changes)
        return out if self.store.put_job_if(out, state=job.state, epoch=job.epoch) else None

    def _fail_range(self, job: Job, error: str, failure_class: str) -> None:
        rng = self._range(job)
        prog = self.progress(job.scope, job.stage)
        if len(rng) or prog.target_offset is not None:
            self._advance(
                job,
                max(job.target_offset, prog.watermark),
                {o: (failure_class, error) for o in rng},
                finish=True,
            )

    def _retry_or_fail(
        self,
        job: Job,
        error: str,
        failure_class: str,
        retryable: bool,
        *,
        backoff: bool = True,
        expect: bool = False,
    ) -> Job | None:
        """Requeue within the budget, else fail and turn the range into holes. With ``expect``
        the transition is conditional (``_put_if``) and None means another process got there
        first; the range is then left to whoever owns the job now."""
        if failure_class not in FAILURE_CLASSES:
            raise ValueError(f"unknown failure class {failure_class!r}")
        attempts = job.attempts + 1
        base: dict[str, Any] = {
            "attempts": attempts,
            "error": error,
            "failure_class": failure_class,
            "lease_owner": None,
            "lease_until": None,
        }
        if retryable and attempts < self.budget.max_attempts:
            wait = self.budget.backoff(attempts) if backoff else timedelta(0)
            if expect:
                return self._put_if(job, "queued", not_before=self.clock() + wait, **base)
            return self._put(job, "queued", not_before=self.clock() + wait, **base)
        if expect:
            failed = self._put_if(job, "failed", **base)
            if failed is not None:
                self._fail_range(job, error, failure_class)
            return failed
        self._fail_range(job, error, failure_class)
        return self._put(job, "failed", **base)

    def release(self, job_id: str, epoch: int) -> FenceResult:
        """Give a running job back to the queue without costing an attempt (drain on stop)."""
        job, refused = self._fence(job_id, "release", epoch)
        if refused is not None or job is None:
            return refused or FenceResult(False, "unknown job")
        return FenceResult(True, None, self._put(job, "queued", lease_owner=None, lease_until=None))

    def cancel(self, job_id: str) -> Job:
        """Cancel a queued or running job. A running job's epoch is bumped so its worker is fenced."""
        job = self.get(job_id)
        if job.state == "running":
            return self._put(
                job, "cancelled", epoch=job.epoch + 1, lease_owner=None, lease_until=None
            )
        return self._put(job, "cancelled")

    # ------------------------------------------------------------------ holes
    def holes_to_retry(self, scope: OwnerScope, stage: str) -> list[Hole]:
        """Open holes still inside the per-hole retry budget (ER-15). A hole whose reason is
        non-retryable (``NON_RETRYABLE_HOLE_REASONS``: an exhausted budget or a deterministic
        fence) is never retried; it stays visible until waived."""
        return [
            h
            for h in self.progress(scope, stage).open_holes
            if h.attempts < self.budget.max_hole_attempts
            and h.reason not in NON_RETRYABLE_HOLE_REASONS
        ]

    def _update_holes(
        self, scope: OwnerScope, stage: str, offset: int, fn: Callable[[Hole], Hole | None]
    ) -> None:
        prog = self.progress(scope, stage)
        if offset not in {h.offset for h in prog.holes}:
            raise KeyError(f"no hole at offset {offset}")
        holes = []
        for h in prog.holes:
            new = fn(h) if h.offset == offset else h
            if new is not None:
                holes.append(new)
        self.store.put_progress(prog.model_copy(update={"holes": holes}))

    def record_hole_result(
        self,
        scope: OwnerScope,
        stage: str,
        offset: int,
        ok: bool,
        error: str | None = None,
        *,
        reason: str | None = None,
    ) -> None:
        """A hole retry finished: success removes the hole, failure counts an attempt. ``reason``
        reclassifies the hole (a retry that met a deterministic fence)."""
        if ok:
            self._update_holes(scope, stage, offset, lambda h: None)
            return
        if reason is not None and reason not in FAILURE_CLASSES:
            raise ValueError(f"unknown failure class {reason!r}")
        update: dict[str, Any] = {"last_error": error}
        if reason is not None:
            update["reason"] = reason
        self._update_holes(
            scope,
            stage,
            offset,
            lambda h: h.model_copy(update={**update, "attempts": h.attempts + 1}),
        )

    def reopen_hole(self, scope: OwnerScope, stage: str, offset: int, note: str) -> bool:
        """Make a ``source_missing`` hole retryable again (its item was rewritten so it no longer
        names the erased source): reason ``raised``, attempts reset. Any other hole is left as it
        is. Returns whether a hole was reopened."""
        hole = next((h for h in self.progress(scope, stage).holes if h.offset == offset), None)
        if hole is None or hole.reason != "source_missing" or hole.waived:
            return False
        self._update_holes(
            scope,
            stage,
            offset,
            lambda h: h.model_copy(update={"reason": "raised", "attempts": 0, "last_error": note}),
        )
        return True

    def waive_hole(self, scope: OwnerScope, stage: str, offset: int) -> None:
        self._update_holes(scope, stage, offset, lambda h: h.model_copy(update={"waived": True}))

    # ------------------------------------------------------------------ drain on stop
    def drain_plan(self, deadline: datetime, estimate: Callable[[Job], float | None]) -> DrainPlan:
        """Running jobs whose estimated remaining seconds fit before ``deadline`` finish; the rest are
        released; queued jobs stay queued. ``None`` estimate means unknown: release."""
        now = self.clock()
        budget = (deadline - now).total_seconds()
        finish, release, queued = [], [], []  # type: ignore[var-annotated]  # CI-GREEN: pre-existing typing debt
        for j in self.store.live_jobs():
            if j.state == "queued":
                queued.append(j.job_id)
            elif j.state == "running":
                est = estimate(j)
                (finish if est is not None and est <= budget else release).append(j.job_id)
        return DrainPlan(deadline, tuple(finish), tuple(release), tuple(queued))

    def apply_drain_plan(self, plan: DrainPlan) -> list[str]:
        """Release the jobs the plan could not finish. Call after the deadline, once ``finish`` jobs
        have committed (those no longer running are skipped)."""
        out = []
        for jid in plan.release + plan.finish:
            j = self.get(jid)
            if j.state == "running" and (jid in plan.release or self.clock() >= plan.deadline):
                self._put(j, "queued", lease_owner=None, lease_until=None)
                out.append(jid)
        return out


# ---------------------------------------------------------------------- triggers (OF-1 / OF-2)
@dataclass(frozen=True)
class TriggerConfig:
    """All thresholds are configuration (OF-2): N deposits, M idle, C pressure, K gap."""

    min_deposits: int = 20  # N
    idle: timedelta = timedelta(minutes=10)  # M
    context_pressure: float = 0.70  # C
    min_gap: timedelta = timedelta(hours=1)  # K, gates both trigger kinds
    maintenance_interval: timedelta = timedelta(hours=24)

    def __post_init__(self) -> None:
        if self.min_deposits < 0:
            raise ValueError("min_deposits must be >= 0")
        if not 0.0 < self.context_pressure <= 1.0:
            raise ValueError("context_pressure must be in (0, 1]")
        if min(self.idle, self.min_gap, self.maintenance_interval) < timedelta(0):
            raise ValueError("durations must be non-negative")


@dataclass(frozen=True)
class TriggerStats:
    deposits_since_run: int = 0
    last_deposit_at: datetime | None = None
    last_run_at: datetime | None = None
    context_pressure: float = 0.0  # working memory / context budget
    last_maintenance_at: datetime | None = None


@dataclass(frozen=True)
class TriggerDecision:
    run: bool
    kind: Literal["sleep", "compression"] | None
    reason: str  # deposits_idle | context_pressure | deposits_idle+context_pressure | gap | idle_wait | not_due
    rearm_at: datetime | None = None  # when to look again (idle expiry or end of gap)


def should_run(
    stats: TriggerStats, now: datetime, cfg: TriggerConfig | None = None
) -> TriggerDecision:
    """(N deposits AND M idle) OR context pressure >= C, both behind the minimum gap K."""
    cfg = cfg or TriggerConfig()
    n_met = stats.deposits_since_run >= cfg.min_deposits and stats.deposits_since_run > 0
    idle_at = None if stats.last_deposit_at is None else stats.last_deposit_at + cfg.idle
    idle_met = idle_at is None or now >= idle_at
    deposit_cond = n_met and idle_met
    pressure_cond = stats.context_pressure >= cfg.context_pressure
    if not (deposit_cond or pressure_cond):
        if n_met and idle_at is not None:
            return TriggerDecision(False, None, "idle_wait", idle_at)
        return TriggerDecision(False, None, "not_due")
    gap_end = None if stats.last_run_at is None else stats.last_run_at + cfg.min_gap
    if gap_end is not None and now < gap_end:
        return TriggerDecision(False, None, "gap", gap_end)
    if deposit_cond and pressure_cond:
        return TriggerDecision(True, "sleep", "deposits_idle+context_pressure")
    if deposit_cond:
        return TriggerDecision(True, "sleep", "deposits_idle")
    return TriggerDecision(True, "compression", "context_pressure")


def maintenance_due(stats: TriggerStats, now: datetime, cfg: TriggerConfig | None = None) -> bool:
    """Daily maintenance (decay sweeps, pruning): due once per ``maintenance_interval``."""
    cfg = cfg or TriggerConfig()
    return (
        stats.last_maintenance_at is None
        or now - stats.last_maintenance_at >= cfg.maintenance_interval
    )


class Triggers:
    """Thin holder binding a config to the pure functions."""

    def __init__(self, cfg: TriggerConfig | None = None) -> None:
        self.cfg = cfg or TriggerConfig()

    def should_run(self, stats: TriggerStats, now: datetime) -> TriggerDecision:
        return should_run(stats, now, self.cfg)

    def maintenance_due(self, stats: TriggerStats, now: datetime) -> bool:
        return maintenance_due(stats, now, self.cfg)
