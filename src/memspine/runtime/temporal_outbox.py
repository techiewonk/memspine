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

Fences become Temporal outcomes, never holes (the fenced offset is never checkpointed):

=========  ===========  ======================================================================
reason     retryable    why
=========  ===========  ======================================================================
lease      yes          our lease ran out and was reclaimed; the next attempt claims afresh
busy       yes, later   another worker holds the job (or its effect events); retry after its
                        lease can have expired
source     yes          a source was erased or changed mid-step; the projection was
                        compensated; the next attempt re-reads the rewritten item
replay     no           a replay planned different effects from the acknowledged ones; retrying
                        the same replay diverges again (operator decision; the job stays queued)
=========  ===========  ======================================================================

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
from memspine.runtime.runner import DrainReport, FenceReason, InlineRunner, _Fenced
from memspine.schemas.durable.common import OwnerScope

__all__ = [
    "OUTBOX_DRAIN_ACTIVITY",
    "OUTBOX_DRAIN_WORKFLOW",
    "InvalidDrainRequest",
    "OutboxActivity",
    "OutboxActivityFenced",
    "TemporalFailure",
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
        return self.reason != "replay"


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


def outbox_workflow_id(deployment_id: str, scope: OwnerScope, stage: str) -> str:
    """One drain workflow per (deployment, scope, stage): duplicate kicks join the running one.
    Bounded and opaque: no tenant or owner text reaches Temporal's visibility store."""
    digest = hashlib.sha256(
        "\0".join((deployment_id, scope.tenant, scope.owner, scope.owner_kind, stage)).encode()
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

        Returns ``{"status", "job_id", "items", "holes", "hole_retries", "hole_filled",
        "watermark", "more"}``; ``status`` is ``idle`` (nothing to do), ``partial`` (job handed
        back, ``more``) or ``drained``. Raises :class:`OutboxActivityFenced` on a fence and
        :class:`InvalidDrainRequest` on bad input. Any other exception (a crash) leaves the lease
        to expire, so a retry reclaims and replays."""
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
            # Hand the job back when it is still ours (no attempt spent). The fenced offset was
            # not checkpointed: it is retried, never a hole.
            await runner._release(claim)
            tap.event("runtime.temporal.fenced", job_id=job_id, stage=stage, reason=fence.reason)
            retry_after = self._lease_wait() if fence.reason == "busy" else None
            raise OutboxActivityFenced(fence.reason, retry_after_s=retry_after) from fence
        except asyncio.CancelledError:
            # Temporal cancelled the Activity (timeout or workflow cancel): give the job back.
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


async def kick_outbox_drain(
    client: Any,
    *,
    deployment_id: str,
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
        from temporalio.common import WorkflowIDConflictPolicy
    except ImportError as exc:
        raise MissingServiceError("Temporal outbox drain", "temporal") from exc
    request = {
        **drain_request(scope, stage, max_items=max_items),
        "activity_timeout_s": float(activity_timeout_s),
        "heartbeat_timeout_s": float(heartbeat_timeout_s),
        "retry_max_attempts": int(retry_max_attempts),
        "max_steps": int(max_steps),
    }
    _parse(request)
    return await client.start_workflow(
        OUTBOX_DRAIN_WORKFLOW,
        request,
        id=outbox_workflow_id(deployment_id, scope, stage),
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )
