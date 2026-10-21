from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from .contracts import Job, Result


@workflow.defn
class ProcessingWorkflow:
    @workflow.run
    async def run(self, job: Job) -> Result:
        result = await workflow.execute_activity(
            "run_micro",
            job,
            result_type=Result,
            start_to_close_timeout=timedelta(minutes=3),
            schedule_to_close_timeout=timedelta(minutes=15),
            heartbeat_timeout=timedelta(seconds=20),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            retry_policy=RetryPolicy(
                initial_interval=timedelta(seconds=2),
                maximum_interval=timedelta(seconds=30),
                maximum_attempts=5,
            ),
        )
        return await workflow.execute_activity(
            "verify_result",
            args=[job, result],
            result_type=Result,
            start_to_close_timeout=timedelta(seconds=15),
            schedule_to_close_timeout=timedelta(minutes=1),
            retry_policy=RetryPolicy(maximum_attempts=3),
        )
