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
