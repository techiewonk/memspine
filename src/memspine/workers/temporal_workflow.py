"""Temporal workflow definition, imported only by the optional Temporal runner."""

from __future__ import annotations

from datetime import timedelta
from typing import Any, TypedDict, cast

from temporalio import workflow
from temporalio.common import RetryPolicy


class PipelineInvocation(TypedDict):
    deployment_id: str
    invocation_id: str
    operation_id: str
    pipeline_name: str
    config_fingerprint: str
    input_fingerprint: str
    activity_timeout_s: float
    retry_max_attempts: int
    step_lease_s: float
    max_result_bytes: int


@workflow.defn(name="memspine.pipeline")
class PipelineWorkflow:
    """Send only immutable invocation identity to the Activity worker."""

    @workflow.run
    async def run(self, invocation: PipelineInvocation) -> dict[str, Any]:
        result = await workflow.execute_activity(
            "memspine.run_pipeline",
            invocation,
            result_type=dict,
            start_to_close_timeout=timedelta(seconds=invocation["activity_timeout_s"]),
            schedule_to_close_timeout=timedelta(
                seconds=(invocation["activity_timeout_s"] * invocation["retry_max_attempts"])
                + invocation["step_lease_s"] * (invocation["retry_max_attempts"] - 1)
            ),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            retry_policy=RetryPolicy(maximum_attempts=invocation["retry_max_attempts"]),
        )
        return cast(dict[str, object], result)


class OutboxDrainRequest(TypedDict):
    """RM-132: identifiers and Temporal budgets only; the outbox holds every payload and state."""

    tenant: str
    owner: str
    owner_kind: str
    stage: str
    max_items: int
    activity_timeout_s: float
    heartbeat_timeout_s: float
    retry_max_attempts: int
    max_steps: int


@workflow.defn(name="memspine.outbox.drain")
class OutboxDrainWorkflow:
    """Drive bounded outbox drain steps for one (scope, stage) until nothing is left.

    Each step is ``runtime.temporal_outbox.OutboxActivity.drain`` (Activity
    ``memspine.outbox.drain_step``): Temporal retries, times out and heartbeats it, and never owns
    state. After ``max_steps`` steps with work left the workflow continues as new, so its history
    stays bounded. A fence the Activity reports as non-retryable fails the workflow with the job
    still queued in the outbox (no hole)."""

    @workflow.run
    async def run(self, request: OutboxDrainRequest) -> dict[str, Any]:
        totals: dict[str, Any] = {"steps": 0, "items": 0, "holes": 0, "watermark": None}
        for _ in range(max(1, request["max_steps"])):
            result = await workflow.execute_activity(
                "memspine.outbox.drain_step",
                request,
                result_type=dict,
                start_to_close_timeout=timedelta(seconds=request["activity_timeout_s"]),
                heartbeat_timeout=timedelta(seconds=request["heartbeat_timeout_s"]),
                cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
                retry_policy=RetryPolicy(maximum_attempts=request["retry_max_attempts"]),
            )
            totals["steps"] += 1
            totals["items"] += int(result.get("items", 0))
            totals["holes"] += int(result.get("holes", 0))
            totals["watermark"] = result.get("watermark")
            if not result.get("more"):
                return totals
        workflow.continue_as_new(request)
        raise AssertionError("continue_as_new does not return")
