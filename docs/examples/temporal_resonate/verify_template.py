"""SDK-level smoke checks only; no Temporal server or external service is used."""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from temporalio import workflow
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner

from .activities import run_micro, verify_result
from .contracts import Job
from .workflows import ProcessingWorkflow


async def verify(journal_path: str) -> None:
    os.environ["MEMSPINE_MICRO_JOURNAL_PATH"] = journal_path
    # Fail if local construction accidentally consults ambient server settings.
    os.environ["RESONATE_URL"] = "http://127.0.0.1:1"
    environment = ActivityEnvironment()
    job = Job("synthetic-tenant", "synthetic-job", [" ALPHA ", "beta", "ALPHA", ""])
    async with asyncio.timeout(15):
        result = await environment.run(run_micro, job)
        assert result.items == ["alpha", "beta"]
        assert await environment.run(verify_result, job, result) == result
        assert await environment.run(run_micro, job) == result
        with closing(sqlite3.connect(journal_path)) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM micro_steps WHERE state = 'COMPLETED'"
                ).fetchone()[0]
                == 3
            )
            assert (
                connection.execute("SELECT max(fencing_token) FROM micro_steps").fetchone()[0] == 1
            )
        for changed in (
            replace(result, namespace="other"),
            replace(result, operation_id="other"),
            replace(result, rounds=2),
            replace(result, items=["tampered"]),
        ):
            try:
                await environment.run(verify_result, job, changed)
            except ApplicationError as exc:
                assert exc.non_retryable
            else:
                raise AssertionError("Altered result was accepted")
        with patch("temporal_resonate.durable.prepare", new=AsyncMock(side_effect=TimeoutError)):
            try:
                await environment.run(run_micro, replace(job, operation_id="uncertain-timeout"))
            except Exception:
                pass
            else:
                raise AssertionError("Expected uncertain leaf timeout")
        with closing(sqlite3.connect(journal_path)) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM micro_steps WHERE state = 'RUNNING'"
                ).fetchone()[0]
                == 1
            )
        try:
            await environment.run(run_micro, Job("", "invalid", []))
        except ApplicationError as exc:
            assert exc.non_retryable
        else:
            raise AssertionError("Invalid input was accepted")
    # Private SDK metadata is used only by this verification tool.
    SandboxedWorkflowRunner().prepare_workflow(
        workflow._Definition.must_from_class(ProcessingWorkflow)
    )
    print(
        "PASS: local processing, verification, committed-step reuse, input rejection, workflow sandbox"
    )
    print(
        "NOT TESTED: live Temporal dispatch, heartbeat failover, live container failover or volume reattachment"
    )


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="memspine-micro-journal-") as directory:
        await verify(str(Path(directory) / "steps.sqlite3"))


if __name__ == "__main__":
    asyncio.run(main())
