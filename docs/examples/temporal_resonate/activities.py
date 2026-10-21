from __future__ import annotations

import asyncio
import hashlib
import json
import os
from dataclasses import asdict
from datetime import timedelta

from temporalio import activity
from temporalio.exceptions import ApplicationError

from .contracts import Job, Result
from .durable import new_durable_local_runtime


def digest(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


async def keep_heartbeating(operation_key: str) -> None:
    while True:
        # Created inside the Activity, preserving Temporal's activity context.
        # This is liveness metadata, NOT a persisted micro-step checkpoint.
        activity.heartbeat({"operation_key": operation_key, "state": "running"})
        await asyncio.sleep(5)


@activity.defn
async def run_micro(job: Job) -> Result:
    if (
        not isinstance(job.namespace, str)
        or not 1 <= len(job.namespace) <= 256
        or not isinstance(job.operation_id, str)
        or not 1 <= len(job.operation_id) <= 256
        or not isinstance(job.items, list)
        or type(job.max_rounds) is not int
        or not 1 <= job.max_rounds <= 8
        or len(job.items) > 128
        or any(not isinstance(item, str) or len(item) > 256 for item in job.items)
    ):
        raise ApplicationError(
            "Invalid processing request", type="InvalidInput", non_retryable=True
        )

    # Business idempotency keys use operation_key, without a Temporal attempt.
    operation_key = digest(
        {
            "deployment": os.environ.get("MEMSPINE_DEPLOYMENT", "development"),
            "namespace": job.namespace,
            "operation": job.operation_id,
            "pipeline": "micro-v1",
        }
    )
    request_fingerprint = digest(asdict(job))
    info = activity.info()
    # A failed local promise is terminal. Each Activity attempt gets a new root;
    # the local graph replays, reusing committed application step outputs.
    invocation_id = digest([operation_key, info.workflow_run_id, info.activity_id, info.attempt])
    local = new_durable_local_runtime(
        os.environ.get("MEMSPINE_MICRO_JOURNAL_PATH", "local_resonate.db"),
        operation_key,
        invocation_id,
        request_fingerprint,
    )  # One owned runtime per Activity attempt; persistent journal shared across retries.
    try:
        async with asyncio.TaskGroup() as tasks:
            heartbeat = tasks.create_task(keep_heartbeating(operation_key))
            try:
                async with asyncio.timeout(100):
                    handle = local.options(timeout=timedelta(seconds=90)).run(
                        invocation_id, "journaled-micro-pipeline-v1", job.items, job.max_rounds
                    )
                    output = await handle.result()
                    return Result(
                        job.namespace,
                        job.operation_id,
                        output["items"],
                        digest(
                            {
                                "job": asdict(job),
                                "items": output["items"],
                                "rounds": output["rounds"],
                            }
                        ),
                        output["rounds"],
                    )
            finally:
                heartbeat.cancel()
    finally:
        # SDK stop drains execution tasks; real leaves must have bounded I/O
        # deadlines and cooperative cancellation. It cannot kill a stuck thread.
        await local.stop()


@activity.defn
async def verify_result(job: Job, result: Result) -> Result:
    expected = digest({"job": asdict(job), "items": result.items, "rounds": result.rounds})
    if (
        result.namespace != job.namespace
        or result.operation_id != job.operation_id
        or type(result.rounds) is not int
        or not 1 <= result.rounds <= job.max_rounds
        or expected != result.digest
    ):
        raise ApplicationError("Result integrity failure", type="InvalidResult", non_retryable=True)
    return result
