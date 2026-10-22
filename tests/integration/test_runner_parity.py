"""Runner parity: the same sleep-cycle work → the same materialized result
under every available runner (D-16).

``inline`` always participates. ``taskiq`` participates against ``fakeredis``
(a dev dependency — the same seam the taskiq unit test drives, so the extra
itself need not be installed). Server-backed Temporal execution is covered by
the separately gated Temporal integration test.

Each runner gets its OWN file-backed database seeded with identical data, so the
comparison is of independent runs of the same idempotent pipelines, not of
shared state. Parity is asserted on both the stats-dict shape (keys ==
SLEEP_CYCLE_ORDER, every stage ok/noop/skipped) and the materialized effect
(records consolidated + records decayed).
"""

from __future__ import annotations

import builtins
import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.config.loader import load_config
from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.derive.pipeline.stages.table import PIPELINES
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, RecordStatus
from memspine.workers.inline import InlineRunner
from memspine.workers.runner import TaskRunner
from memspine.workers.schedule import SLEEP_CYCLE_ORDER, run_sleep_cycle

_OK = {"ok", "noop", "skipped"}
_SESSION_START = datetime(2025, 1, 1, 9, 0, 0, tzinfo=UTC)
_ANCIENT = datetime(2019, 1, 1, 9, 0, 0, tzinfo=UTC)


async def _build_ctx(path: str) -> tuple[PipelineContext, SQLiteStorage, SQLiteClient]:
    """A file-backed pipeline context with a real RecordProjector behind the
    append-and-project door — the shape the engine injects, materialized to disk."""
    client = SQLiteClient(SQLiteSpec(path))
    await client.open()
    storage = SQLiteStorage(client, mode=EventLogMode.FULL)
    await storage.start()
    projector = RecordProjector(storage)

    async def append(event: MemoryEvent) -> None:
        appended = await storage.append_event(event)
        assert appended.seq is not None
        await projector.apply(appended)
        await storage.set_offset(projector.name, appended.seq)

    # Runtime lifecycle/service timeouts are required in raw configuration;
    # load an explicit shipped config rather than relying on implicit defaults.
    config = load_config(template="base").config
    return PipelineContext(storage=storage, config=config, append_event=append), storage, client


async def _seed(ctx: PipelineContext) -> None:
    async def episode(content: str, when: datetime) -> None:
        record = MemoryRecord(
            namespace="default",
            memory_type="episodic",
            content=content,
            valid_from=when,
            recorded_at=when,
        )
        assert ctx.append_event is not None
        await ctx.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="default",
                actor="test",
                payload={"record": record.model_dump(mode="json")},
            )
        )

    await episode("alice arrived. detail", _SESSION_START)
    await episode("bob spoke. detail", _SESSION_START + timedelta(minutes=2))
    await episode("deal closed. detail", _SESSION_START + timedelta(minutes=4))
    await episode("ancient chatter. detail", _ANCIENT)


async def _summary_count(storage: SQLiteStorage) -> int:
    records = await storage.list_records("default", "semantic")
    return sum(
        1
        for record in records
        if record.source.channel == "consolidation" and record.status is RecordStatus.ACTIVATED
    )


def _available_runners() -> dict[str, TaskRunner]:
    runners: dict[str, TaskRunner] = {"inline": InlineRunner()}
    try:
        import fakeredis.aioredis

        from memspine.workers.taskiq_runner import TaskiqRunner

        runners["taskiq"] = TaskiqRunner(redis=fakeredis.aioredis.FakeRedis())
    except ImportError:  # pragma: no cover - fakeredis is a dev dependency
        pass
    return runners


async def test_sleep_cycle_parity_across_available_runners(tmp_path: Path) -> None:
    runners = _available_runners()
    effects: dict[str, tuple[int, int]] = {}

    for name, runner in runners.items():
        for pipeline_name, pipeline in PIPELINES.items():
            runner.register(pipeline_name, pipeline)
        ctx, storage, client = await _build_ctx(str(tmp_path / f"{name}.db"))
        try:
            await _seed(ctx)
            stats = await run_sleep_cycle(runner, ctx)

            # Stats-dict shape parity: one entry per ordered stage, all healthy.
            assert set(stats) == set(SLEEP_CYCLE_ORDER), name
            assert all(stage["status"] in _OK for stage in stats.values()), (name, stats)

            summaries = await _summary_count(storage)
            transitions = int(stats["decay_sweep"].get("transitions", 0))
            assert summaries == 1, name  # the one closed session was consolidated
            assert transitions > 0, name  # the ancient record decayed
            effects[name] = (summaries, transitions)
        finally:
            await runner.close()
            await storage.stop()
            await client.close()

    # Effect parity: every available runner materialized the identical result.
    assert "inline" in effects
    baseline = effects["inline"]
    for name, effect in effects.items():
        assert effect == baseline, f"{name} diverged from inline: {effect} != {baseline}"

    if len(effects) == 1:  # only inline available in this env — record why
        pytest.skip(
            "only the inline runner is available (fakeredis/taskiq not installed) — parity trivial"
        )


async def test_temporal_runner_dispatches_across_worker_restart(tmp_path: Path) -> None:
    """Exercise the external Temporal server and reopen the same local journal."""
    # tests/conftest.py scrubs MEMSPINE_* configuration variables; the test-only
    # prefix is deliberately preserved for explicit live service gates.
    address = os.environ.get("MEMSPINE_TEST_TEMPORAL_ADDRESS")
    if not address:
        pytest.skip("set MEMSPINE_TEST_TEMPORAL_ADDRESS to run the live Temporal integration gate")
    pytest.importorskip("temporalio", reason="Temporal SDK extra is not installed")
    from temporalio.client import WorkflowFailureError

    from memspine.workers.temporal_runner import TemporalRunner, fingerprint_input

    executions: list[int] = []

    async def probe(_ctx: PipelineContext) -> dict[str, object]:
        executions.append(len(executions) + 1)
        return {"status": "ok", "execution": executions[-1]}

    ctx, storage, client = await _build_ctx(str(tmp_path / "temporal-engine.db"))
    journal_path = tmp_path / "temporal-journal.sqlite"
    task_queue = f"memspine-test-{uuid.uuid4().hex}"
    deployment_id = f"temporal-test-{uuid.uuid4().hex}"

    def create_runner() -> Any:
        runner = TemporalRunner(
            address=address,
            namespace=os.environ.get("MEMSPINE_TEMPORAL_NAMESPACE", "default"),
            task_queue=task_queue,
            deployment_id=deployment_id,
            journal_path=str(journal_path),
            context_factory=lambda: ctx,
            rpc_timeout_s=30,
            workflow_timeout_s=240,
            activity_timeout_s=60,
            max_concurrent_activities=1,
            retry_max_attempts=1,
            resonate_task_timeout_s=20,
            step_timeout_s=15,
            step_lease_s=90,
        )
        runner.register("journal-smoke-v1", probe)
        return runner

    try:
        first_worker = create_runner()
        try:
            await first_worker.start()
            first_result = await first_worker.run(
                "journal-smoke-v1",
                ctx,
                operation_id="same-business-operation",
                input_fingerprint=fingerprint_input({"source": "business-payload-v1"}),
            )
        finally:
            await first_worker.close()
        assert first_result == {"status": "ok", "execution": 1}
        assert executions == [1]
        assert journal_path.is_file()
        with sqlite3.connect(journal_path) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM micro_steps WHERE state = 'COMPLETED'"
                ).fetchone()[0]
                == 1
            )

        second_worker = create_runner()
        try:
            await second_worker.start()
            second_result = await second_worker.run(
                "journal-smoke-v1",
                ctx,
                operation_id="same-business-operation",
                input_fingerprint=fingerprint_input({"source": "business-payload-v1"}),
            )
        finally:
            await second_worker.close()
        assert second_result == first_result
        assert executions == [1]  # completed same-operation output was reused
        assert journal_path.is_file()
        with sqlite3.connect(journal_path) as connection:
            assert (
                connection.execute(
                    "SELECT count(*) FROM micro_steps WHERE state = 'COMPLETED'"
                ).fetchone()[0]
                == 1
            )

        changed_input_worker = create_runner()
        try:
            await changed_input_worker.start()
            with pytest.raises(WorkflowFailureError):
                await changed_input_worker.run(
                    "journal-smoke-v1",
                    ctx,
                    operation_id="same-business-operation",
                    input_fingerprint=fingerprint_input({"source": "business-payload-v2"}),
                )
        finally:
            await changed_input_worker.close()
        assert executions == [1]
    finally:
        await storage.stop()
        await client.close()


async def test_temporal_retry_reclaims_failed_pipeline_lease(tmp_path: Path) -> None:
    """A real Temporal Activity retry reclaims the local journal lease."""
    address = os.environ.get("MEMSPINE_TEST_TEMPORAL_ADDRESS")
    if not address:
        pytest.skip("set MEMSPINE_TEST_TEMPORAL_ADDRESS to run the live Temporal integration gate")
    pytest.importorskip("temporalio", reason="Temporal SDK extra is not installed")

    from memspine.workers.temporal_runner import TemporalRunner, fingerprint_input

    calls = 0

    async def fail_once(_ctx: PipelineContext) -> dict[str, object]:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("transient failure before side effect")
        return {"status": "ok", "calls": calls}

    ctx, storage, client = await _build_ctx(str(tmp_path / "temporal-retry-engine.db"))
    journal_path = tmp_path / "temporal-retry-journal.sqlite"
    runner = TemporalRunner(
        address=address,
        namespace=os.environ.get("MEMSPINE_TEST_TEMPORAL_NAMESPACE", "default"),
        task_queue=f"memspine-retry-test-{uuid.uuid4().hex}",
        deployment_id=f"temporal-retry-test-{uuid.uuid4().hex}",
        journal_path=str(journal_path),
        context_factory=lambda: ctx,
        rpc_timeout_s=10,
        workflow_timeout_s=30,
        activity_timeout_s=1,
        max_concurrent_activities=1,
        retry_max_attempts=3,
        resonate_task_timeout_s=0.4,
        step_timeout_s=0.2,
        step_lease_s=1.5,
        journal_busy_timeout_s=0.05,
    )
    runner.register("retry-smoke-v1", fail_once)
    try:
        await runner.start()
        result = await runner.run(
            "retry-smoke-v1",
            ctx,
            operation_id="same-retry-operation",
            input_fingerprint=fingerprint_input({"source": "transient-retry-payload"}),
        )
        assert result == {"status": "ok", "calls": 2}
        assert calls == 2
        with sqlite3.connect(journal_path) as connection:
            rows = connection.execute("SELECT state, fencing_token FROM micro_steps").fetchall()
        assert rows == [("COMPLETED", 2)]
    finally:
        await runner.close()
        await storage.stop()
        await client.close()


@pytest.mark.parametrize(
    "error",
    [
        ImportError("internal adapter broken"),
        ModuleNotFoundError(name="memspine.workers.temporal_runner"),
    ],
)
async def test_temporal_gate_does_not_skip_internal_import_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, error: ImportError
) -> None:
    """A configured service and available SDK must expose internal import defects."""
    monkeypatch.setenv("MEMSPINE_TEST_TEMPORAL_ADDRESS", "unused.invalid:7233")
    original_import = builtins.__import__

    def sdk_available(name: str, **kwargs: Any) -> object:
        assert name == "temporalio"  # internal adapters must never use importorskip
        return object()

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "temporalio.client":
            return SimpleNamespace(WorkflowFailureError=RuntimeError)
        if name == "memspine.workers.temporal_runner":
            raise error
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(pytest, "importorskip", sdk_available)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    with pytest.raises(type(error)) as caught:
        await test_temporal_runner_dispatches_across_worker_restart(tmp_path)
    assert caught.value is error
