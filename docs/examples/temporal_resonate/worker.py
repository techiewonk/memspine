from __future__ import annotations

import asyncio
import os
from datetime import timedelta

from temporalio.client import Client
from temporalio.worker import Worker

from .activities import run_micro, verify_result
from .workflows import ProcessingWorkflow


async def main() -> None:
    tls = os.environ.get("TEMPORAL_TLS", "false").lower() == "true"
    if os.environ.get("APP_ENV") == "production" and not tls:
        raise RuntimeError("Configure Temporal TLS/authentication for production")
    if os.environ.get("APP_ENV") == "production" and not os.environ.get(
        "MEMSPINE_MICRO_JOURNAL_PATH"
    ):
        raise RuntimeError("Configure the journal path on a persistent worker volume")
    client = await Client.connect(
        os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"),
        namespace=os.environ.get("TEMPORAL_NAMESPACE", "default"),
        tls=tls,
        api_key=os.environ.get("TEMPORAL_API_KEY"),
    )
    worker = Worker(
        client,
        task_queue=os.environ.get("TEMPORAL_TASK_QUEUE", "memspine-processing-v1"),
        workflows=[ProcessingWorkflow],
        activities=[run_micro, verify_result],
        max_concurrent_activities=8,
        graceful_shutdown_timeout=timedelta(seconds=30),
    )
    await worker.run()  # Activities create and close strictly local Resonate runtimes.


if __name__ == "__main__":
    asyncio.run(main())
