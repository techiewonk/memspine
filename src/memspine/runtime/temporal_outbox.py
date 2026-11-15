"""Temporal drains the ingestion outbox (RM-132, ADR-091 / D-115).

The outbox (``runtime.outbox``) stays the only durable queue and the only state. Temporal adds
retries, timeouts, heartbeats and visibility around ONE plain step, :meth:`OutboxActivity.drain`:

1. reconcile the stage for the scope (missing jobs for durable items) and retry open holes inside
   their budget, exactly as an inline drain does;
2. claim the scope's job under a fresh outbox lease (epoch + 1);
3. walk at most ``max_items`` offsets through the inline runner's fenced path (``prepare`` with no
   lock, ``authorize`` against lease and source snapshots, idempotent ``commit``, ``finalize``,
   ``compensate`` when a source was erased meanwhile), checkpointing the watermark after each;
4. commit the job when its frozen target is reached, otherwise hand it back (``release``, no
   attempt spent) and answer ``more=True`` so the workflow schedules the next step.

Nothing here is a second code path: the per-offset work is ``InlineRunner._run_claim``, so a
switch between the inline runner, the taskiq transport and this Activity loses and repeats nothing
beyond what the lease epoch and the effect door already make safe.

Transient fences become retryable Temporal outcomes, never holes (the fenced offset is never
checkpointed). Deterministic fences (RM-147) are not Temporal failures at all: the inline path
records the offset as a waivable hole, logs an alert, and the step goes on past it:

===============  ===========  ==================================================================
reason           outcome      why
===============  ===========  ==================================================================
lease            retryable    our lease ran out and was reclaimed; the next attempt claims afresh
busy             retryable,   another worker holds the job (or its effect events); retry after
                 later        its lease can have expired
source           retryable,   a source changed mid-step (the projection was compensated) or a
                 1 s delay    membership is not accepted yet; the next attempt re-reads the item
replay_diverged  hole         a replay planned different effects from the acknowledged ones;
                              every retry diverges again, so the watermark passes a hole
source_missing   hole         the item still names an erased source; it can never be authorized
===============  ===========  ==================================================================

The step result counts those holes by reason (``hole_fences``).

A crash inside the step (process gone, Activity timed out) leaves the lease to expire; the retry
reclaims the job (``JobBook.expire_leases``) and replays the offset, whose effect door re-authorizes
under the new epoch and whose provider deposits are idempotent, so the effect lands once.

``temporalio`` is optional (``[temporal]``): only :meth:`OutboxActivity.temporal_activity` (when
Temporal calls it) and :func:`kick_outbox_drain` import it. The plain step is testable without it.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from memspine.exceptions import ConfigError, MissingServiceError
from memspine.observability import tap
from memspine.runtime.runner import DrainReport, FenceReason, InlineRunner, _Fenced, _Stop
from memspine.schemas.durable.common import OwnerScope

__all__ = [
    "OUTBOX_DRAIN_ACTIVITY",
    "OUTBOX_DRAIN_WORKFLOW",
    "InvalidDrainRequest",
    "OutboxActivity",
    "OutboxActivityFenced",
    "TemporalFailure",
    "TemporalOutboxRunner",
    "drain_request",
    "kick_outbox_drain",
    "outbox_workflow_id",
    "temporal_failure",
]

#: Names Temporal sees. ``workers.temporal_workflow.OutboxDrainWorkflow`` uses the same literals.
OUTBOX_DRAIN_ACTIVITY = "memspine.outbox.drain_step"
OUTBOX_DRAIN_WORKFLOW = "memspine.outbox.drain"

_DEFAULT_MAX_ITEMS = 16
_MAX_ITEMS_CAP = 1024
#: A source fence is retried promptly: the erasure that caused it has already committed.
_SOURCE_RETRY_S = 1.0


class InvalidDrainRequest(ConfigError):
    """The Activity input is not a drain request this worker can serve (never retried)."""


class OutboxActivityFenced(Exception):
    """A drain step met a fence. The offset was not checkpointed (no hole) and the job was handed
    back when this attempt still owned it."""

    def __init__(self, reason: FenceReason, *, retry_after_s: float | None = None) -> None:
        super().__init__(f"outbox drain fenced: {reason}")
        self.reason: FenceReason = reason
        self.retry_after_s = retry_after_s

    @property
    def retryable(self) -> bool:
        """Every fence that reaches Temporal is transient (RM-147): deterministic fences are
        recorded as holes by the drain step and never raised."""
        return True


@dataclass(frozen=True)
class TemporalFailure:
    """How a fence surfaces to Temporal (``ApplicationError`` arguments), kept plain for tests."""

    type: str
    non_retryable: bool
    next_retry_delay_s: float | None


def temporal_failure(fence: OutboxActivityFenced) -> TemporalFailure:
    """Map a fence to its Temporal outcome (the table in the module docstring)."""
    delay = fence.retry_after_s
    if fence.reason == "source" and delay is None:
        delay = _SOURCE_RETRY_S
    return TemporalFailure(
        type=f"OutboxFenced.{fence.reason}",
        non_retryable=not fence.retryable,
        next_retry_delay_s=None if not fence.retryable else delay,
    )


def drain_request(
    scope: OwnerScope, stage: str, *, max_items: int = _DEFAULT_MAX_ITEMS
) -> dict[str, Any]:
    """The JSON Activity input for one drain step: identifiers only, never payloads or handles."""
    return {
        "tenant": scope.tenant,
        "owner": scope.owner,
        "owner_kind": scope.owner_kind,
        "stage": stage,
        "max_items": max_items,
    }


def _parse(request: object) -> tuple[OwnerScope, str, int]:
    if not isinstance(request, Mapping):
        raise InvalidDrainRequest("outbox drain request must be an object")
    stage = request.get("stage")
    max_items = request.get("max_items", _DEFAULT_MAX_ITEMS)
    if not isinstance(stage, str) or not stage.strip():
        raise InvalidDrainRequest("outbox drain request needs a stage")
    if type(max_items) is not int or not 1 <= max_items <= _MAX_ITEMS_CAP:
        raise InvalidDrainRequest(f"max_items must be an integer in [1, {_MAX_ITEMS_CAP}]")
    try:
        scope = OwnerScope.model_validate(
            {
                "tenant": request.get("tenant"),
                "owner": request.get("owner"),
                "owner_kind": request.get("owner_kind", "user"),
            }
        )
    except ValueError as exc:
        raise InvalidDrainRequest("outbox drain request has an invalid scope") from exc
    return scope, stage, max_items


def outbox_workflow_id(
    deployment_id: str, scope: OwnerScope, stage: str, config_fingerprint: str
) -> str:
    """One drain workflow per (deployment, backend config, scope, stage).

    Duplicate kicks for the same backend join the running one; different backends
    never join one another's workflow even when a deployment id was reused.
    Bounded and opaque: no tenant or owner text reaches Temporal's visibility store."""
    digest = hashlib.sha256(
        "\0".join(
            (deployment_id, config_fingerprint, scope.tenant, scope.owner, scope.owner_kind, stage)
        ).encode()
    ).hexdigest()[:32]
    return f"memspine-outbox-{digest}"


class OutboxActivity:
    """The Temporal Activity over an outbox runner that holds the stage handlers.

    ``runner`` is an :class:`InlineRunner` (or ``TransportRunner``) opened on the SAME store as the
    producers, with the same stages registered: it is used for its fenced per-offset path only;
    its own drain loop is not run."""

    def __init__(self, runner: InlineRunner, *, owner_prefix: str = "temporal") -> None:
        if not isinstance(runner, InlineRunner):
            raise ConfigError("OutboxActivity needs an outbox InlineRunner")
        if not owner_prefix.strip():
            raise ConfigError("owner_prefix must be non-empty")
        self.runner = runner
        self.owner_prefix = owner_prefix

    # ------------------------------------------------------------------ the plain step
    async def drain(
        self,
        request: Mapping[str, Any],
        *,
        owner: str | None = None,
        heartbeat: Callable[[dict[str, Any]], None] | None = None,
        retry_holes: bool = True,
    ) -> dict[str, Any]:
        """Run one bounded, fenced drain step for ``request`` (see the module docstring).

        Returns ``{"status", "job_id", "items", "holes", "hole_fences", "hole_retries",
        "hole_filled", "watermark", "more"}`` (``hole_fences``: deterministic-fence holes recorded
        by this step, by reason); ``status`` is ``idle`` (nothing to do), ``partial`` (job handed
        back, ``more``) or ``drained``. Raises :class:`OutboxActivityFenced` on a fence and
        :class:`InvalidDrainRequest` on bad input. Any other exception (a crash) leaves the lease
        to expire, so a retry reclaims and replays."""
        runner = self.runner
        if isinstance(runner, TemporalOutboxRunner):
            runner._validate_request(request)
            runner._activity_enter()
        try:
            return await self._drain_step(
                request, owner=owner, heartbeat=heartbeat, retry_holes=retry_holes
            )
        finally:
            if isinstance(runner, TemporalOutboxRunner):
                runner._activity_exit()

    async def _drain_step(
        self,
        request: Mapping[str, Any],
        *,
        owner: str | None,
        heartbeat: Callable[[dict[str, Any]], None] | None,
        retry_holes: bool,
    ) -> dict[str, Any]:
        scope, stage, max_items = _parse(request)
        runner = self.runner
        if stage not in runner.stages:
            raise InvalidDrainRequest(f"this worker has no handler for stage {stage!r}")
        lease_owner = (owner or f"{self.owner_prefix}:{runner.owner}")[:512]
        report = DrainReport()
        await runner._reconcile_stage(scope, stage)
        if retry_holes:
            try:
                await runner._retry_holes(scope, stage, report)
            except _Fenced as fence:  # a hole retry lost its own lease mid-checkpoint
                raise OutboxActivityFenced(fence.reason) from fence
        claim = await runner._io(
            runner.book.claim, lease_owner, runner.lease_ttl, scope=scope, stage=stage
        )
        if claim is None:
            busy = self._busy_for(scope, stage)
            if busy is not None:
                raise OutboxActivityFenced("busy", retry_after_s=busy)
            return self._result("idle", None, scope, stage, report, more=False)
        job_id, epoch = claim.job.job_id, claim.lease.epoch
        tap.event(
            "runtime.temporal.claim",
            job_id=job_id,
            stage=stage,
            epoch=epoch,
            target_offset=claim.job.target_offset,
        )

        def beat(offset: int) -> None:
            if heartbeat is not None:
                heartbeat({"job_id": job_id, "epoch": epoch, "offset": offset})

        try:
            finished = await runner._run_claim(claim, report, limit=max_items, on_offset=beat)
        except _Fenced as fence:
            # A transient fence: hand the job back when it is still ours (no attempt spent).
            # The fenced offset was not checkpointed: it is retried, never a hole.
            await runner._release(claim)
            tap.event("runtime.temporal.fenced", job_id=job_id, stage=stage, reason=fence.reason)
            retry_after = self._lease_wait() if fence.reason == "busy" else None
            raise OutboxActivityFenced(fence.reason, retry_after_s=retry_after) from fence
        except asyncio.CancelledError:
            # Temporal cancelled the Activity (timeout or workflow cancel): give the job back.
            await runner._release(claim)
            raise
        except _Stop:
            await runner._release(claim)
            raise
        if not finished and not await runner._release(claim):
            # The release was refused: our lease is gone, the batch already checkpointed.
            raise OutboxActivityFenced("lease")
        report.jobs += 1
        status = "drained" if finished else "partial"
        more = not finished or self._pending(scope, stage)
        return self._result(status, job_id, scope, stage, report, more=more)

    def _pending(self, scope: OwnerScope, stage: str) -> bool:
        book = self.runner.book
        return self.runner.store.last_offset(scope, stage) > book.progress(scope, stage).watermark

    def _busy_for(self, scope: OwnerScope, stage: str) -> float | None:
        """Seconds until work this step could not claim becomes claimable: a live lease held by
        another worker may expire, or a retry backoff (``not_before``) ends. None if no work waits."""
        now = self.runner.clock()
        waits: list[float] = []
        for job in self.runner.book.live_jobs(scope, stage):
            if job.state == "running" and job.lease_until is not None:
                waits.append(max(0.0, (job.lease_until - now).total_seconds()))
            elif job.state == "queued" and job.not_before is not None and job.not_before > now:
                waits.append((job.not_before - now).total_seconds())
        return max(waits) if waits else None

    def _lease_wait(self) -> float:
        return self.runner.lease_ttl.total_seconds()

    def _result(
        self,
        status: str,
        job_id: str | None,
        scope: OwnerScope,
        stage: str,
        report: DrainReport,
        *,
        more: bool,
    ) -> dict[str, Any]:
        return {
            "status": status,
            "job_id": job_id,
            "items": report.items,
            "holes": report.holes,
            "hole_fences": dict(report.hole_fences),
            "hole_retries": report.hole_retries,
            "hole_filled": report.hole_filled,
            "watermark": self.runner.book.progress(scope, stage).watermark,
            "more": more,
        }

    # ------------------------------------------------------------------ the Temporal adapter
    async def temporal_activity(self, request: dict[str, Any]) -> dict[str, Any]:
        """The Activity body Temporal invokes (register it as ``OUTBOX_DRAIN_ACTIVITY``).

        Heartbeats per offset, names the lease after the Activity attempt, and turns fences and bad
        input into ``ApplicationError`` with the retry disposition of :func:`temporal_failure`."""
        try:
            from temporalio import activity
            from temporalio.exceptions import ApplicationError
        except ImportError as exc:  # pragma: no cover - depends on the extra
            raise MissingServiceError("Temporal outbox drain", "temporal") from exc
        info = activity.info()
        owner = (f"{self.owner_prefix}:{info.workflow_run_id}:{info.activity_id}:{info.attempt}")[
            :512
        ]
        try:
            return await self.drain(request, owner=owner, heartbeat=activity.heartbeat)
        except OutboxActivityFenced as fence:
            failure = temporal_failure(fence)
            delay = failure.next_retry_delay_s
            raise ApplicationError(
                str(fence),
                type=failure.type,
                non_retryable=failure.non_retryable,
                next_retry_delay=None if delay is None else timedelta(seconds=delay),
            ) from fence
        except InvalidDrainRequest as exc:
            raise ApplicationError(str(exc), type="InvalidOutboxDrain", non_retryable=True) from exc

    def attach(self, temporal_runner: Any) -> None:
        """Register this Activity (and, through it, the drain workflow) on a ``TemporalRunner``
        before ``start``."""
        register = getattr(temporal_runner, "register_activity", None)
        if not callable(register):
            raise ConfigError("attach needs a TemporalRunner with register_activity")
        register(OUTBOX_DRAIN_ACTIVITY, self.temporal_activity)


class TemporalOutboxRunner(InlineRunner):
    """Producer-side outbox transport. The Activity uses this runner's plain fenced step.

    Window ingestion owns the handlers and calls ``drain`` as before. Only the transport
    changes: a drain awaits one workflow per durable scope, discovery before windows.
    """

    def __init__(
        self,
        *args: Any,
        deployment_id: str,
        config_fingerprint: str,
        activity_timeout_s: float,
        retry_max_attempts: int,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.deployment_id = deployment_id
        if len(config_fingerprint) != 64 or any(
            char not in "0123456789abcdef" for char in config_fingerprint
        ):
            raise ConfigError("Temporal outbox config_fingerprint must be a SHA-256 digest")
        self.config_fingerprint = config_fingerprint
        self.activity_timeout_s = activity_timeout_s
        self.retry_max_attempts = retry_max_attempts
        self._temporal_runner: Any = None
        self._activity_tasks: set[asyncio.Task[Any]] = set()
        self._io_tasks: set[asyncio.Task[Any]] = set()

    def _activity_enter(self) -> None:
        if self._stopping:
            raise RuntimeError("Temporal outbox runner is stopping")
        task = asyncio.current_task()
        if task is None:
            raise RuntimeError("Temporal outbox Activity needs an asyncio task")
        self._activity_tasks.add(task)

    def _activity_exit(self) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._activity_tasks.discard(task)

    async def _io(self, fn: Callable[..., Any], *args: Any, **kw: Any) -> Any:
        # Shield SQL work from Activity cancellation. asyncio.to_thread continues
        # running after its caller is cancelled; the tracked task lets stop wait
        # for that thread before the outbox store can close.
        task = asyncio.create_task(super()._io(fn, *args, **kw))
        self._io_tasks.add(task)
        task.add_done_callback(self._io_tasks.discard)
        return await asyncio.shield(task)

    def bind(self, temporal_runner: Any) -> None:
        """Attach before the Temporal worker starts and before recovery can run."""
        if self._temporal_runner is not None:
            raise ConfigError("Temporal outbox runner is already bound")
        OutboxActivity(self).attach(temporal_runner)
        self._temporal_runner = temporal_runner

    def request(
        self, scope: OwnerScope, stage: str, *, max_items: int = _DEFAULT_MAX_ITEMS
    ) -> dict[str, Any]:
        """The fenced Activity identity for this deployment and configured backend."""
        return {
            **drain_request(scope, stage, max_items=max_items),
            "deployment_id": self.deployment_id,
            "config_fingerprint": self.config_fingerprint,
        }

    def _validate_request(self, request: Mapping[str, Any]) -> None:
        if not isinstance(request, Mapping):
            raise InvalidDrainRequest("outbox drain request must be an object")
        if (
            request.get("deployment_id") != self.deployment_id
            or request.get("config_fingerprint") != self.config_fingerprint
        ):
            raise InvalidDrainRequest("outbox drain deployment or backend identity mismatch")

    async def start(self) -> DrainReport:
        self._stopping = False
        return await self.drain()

    async def stop(self, deadline_s: float = 5.0) -> DrainReport:
        self._stopping = True
        active = tuple(self._activity_tasks)
        if active:
            _, pending = await asyncio.wait(active, timeout=max(deadline_s, 0.0))
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        while self._io_tasks:
            await asyncio.gather(*tuple(self._io_tasks), return_exceptions=True)
        return await super().stop(0)

    def close(self) -> None:
        if self._activity_tasks or self._io_tasks:
            raise RuntimeError("cannot close outbox store while an Activity uses it")
        super().close()

    async def drain(self, scope: OwnerScope | None = None) -> DrainReport:
        temporal_runner = self._temporal_runner
        if temporal_runner is None or temporal_runner.client is None:
            raise RuntimeError("Temporal outbox drain requires a started worker")
        report = DrainReport()
        # A discovery Activity can append window items. Recompute the latter's scopes
        # after all discovery workflows finish, including namespaces unseen this run.
        for stage in self.stages:
            if self._stopping:
                report.stopped = True
                break
            candidates = (
                [scope]
                if scope is not None
                else [
                    *self.store.item_scopes(stage),
                    *self.store.hole_scopes(stage),
                    *(candidate for candidate, kind in self.store.live_scopes() if kind == stage),
                ]
            )
            seen: set[tuple[str, str, str]] = set()
            for candidate in candidates:
                key = (candidate.tenant, candidate.owner, candidate.owner_kind)
                if key in seen:
                    continue
                seen.add(key)
                # A duplicate kick may join a workflow just as its last Activity
                # completes. Recheck the durable watermark and start a new run if
                # additional items arrived in that gap.
                while not self._stopping:
                    top = self.store.last_offset(candidate, stage)
                    progress = self.book.progress(candidate, stage)
                    holes = any(not hole.waived for hole in progress.holes)
                    if top <= progress.watermark and not holes:
                        break
                    handle = await kick_outbox_drain(
                        temporal_runner.client,
                        deployment_id=self.deployment_id,
                        config_fingerprint=self.config_fingerprint,
                        task_queue=temporal_runner.task_queue,
                        scope=candidate,
                        stage=stage,
                        activity_timeout_s=self.activity_timeout_s,
                        heartbeat_timeout_s=min(60.0, self.activity_timeout_s / 2),
                        retry_max_attempts=self.retry_max_attempts,
                    )
                    result = await handle.result()
                    report.jobs += int(result.get("steps", 0))
                    report.items += int(result.get("items", 0))
                    report.holes += int(result.get("holes", 0))
                    # Holes are retried by the Activity within their budget. If
                    # the workflow returned idle with an open hole, a fresh kick
                    # would only repeat that same retry immediately.
                    if (
                        self.store.last_offset(candidate, stage)
                        <= self.book.progress(candidate, stage).watermark
                    ):
                        break
        return report


async def kick_outbox_drain(
    client: Any,
    *,
    deployment_id: str,
    config_fingerprint: str,
    task_queue: str,
    scope: OwnerScope,
    stage: str,
    max_items: int = _DEFAULT_MAX_ITEMS,
    activity_timeout_s: float = 300.0,
    heartbeat_timeout_s: float = 60.0,
    retry_max_attempts: int = 5,
    max_steps: int = 100,
) -> Any:
    """Start the per-scope drain workflow, or join the one already running (a duplicate kick is
    a no-op). Like a taskiq message, a kick only wakes a drainer: losing one loses nothing, the
    rows are durable and the next kick (or an inline drain) finds them."""
    try:
        from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
    except ImportError as exc:
        raise MissingServiceError("Temporal outbox drain", "temporal") from exc
    request = {
        **drain_request(scope, stage, max_items=max_items),
        "deployment_id": deployment_id,
        "config_fingerprint": config_fingerprint,
        "activity_timeout_s": float(activity_timeout_s),
        "heartbeat_timeout_s": float(heartbeat_timeout_s),
        "retry_max_attempts": int(retry_max_attempts),
        "max_steps": int(max_steps),
    }
    _parse(request)
    if len(config_fingerprint) != 64 or any(
        char not in "0123456789abcdef" for char in config_fingerprint
    ):
        raise InvalidDrainRequest("config_fingerprint must be a SHA-256 digest")
    return await client.start_workflow(
        OUTBOX_DRAIN_WORKFLOW,
        request,
        id=outbox_workflow_id(deployment_id, scope, stage, config_fingerprint),
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
        id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
    )
