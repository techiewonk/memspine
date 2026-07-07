"""Workers seam (D-16/D-17): inline runner, dead-letter, sleep cycle, prune."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import load_config
from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers.inline import InlineRunner
from memspine.workers.pipelines import PIPELINES, PipelineContext
from memspine.workers.schedule import SLEEP_CYCLE_ORDER, run_sleep_cycle


async def make_ctx(mode: str = "rolling", retention_days: int = 1) -> PipelineContext:
    config = load_config(
        overrides={"event_log": {"mode": mode, "retention_days": retention_days}}
    ).config
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client, mode=EventLogMode(mode))
    await storage.start()
    return PipelineContext(storage=storage, config=config)


def make_runner() -> InlineRunner:
    runner = InlineRunner()
    for name, pipeline in PIPELINES.items():
        runner.register(name, pipeline)
    return runner


async def test_pipelines_contain_no_runner_imports() -> None:
    """D-17 anti-lock-in: pipelines.py must not import any runner module."""
    import inspect

    import memspine.workers.pipelines as pipelines

    source = inspect.getsource(pipelines)
    for forbidden in ("inline", "dbos", "taskiq", "runner"):
        assert f"workers.{forbidden}" not in source


async def test_sleep_cycle_runs_all_steps_idempotently() -> None:
    ctx = await make_ctx()
    runner = make_runner()
    results = await run_sleep_cycle(runner, ctx)
    assert set(results) == set(SLEEP_CYCLE_ORDER)
    assert all(stats["status"] in {"ok", "noop", "skipped"} for stats in results.values())
    # Idempotent: a second cycle is equally safe.
    assert await run_sleep_cycle(runner, ctx) is not None


async def test_prune_pipeline_respects_mode_and_offsets() -> None:
    ctx = await make_ctx(mode="rolling", retention_days=1)
    event = MemoryEvent(kind=EventKind.WRITE, namespace="n", payload={})
    appended = await ctx.storage.append_event(event)
    assert appended.seq == 1

    # Age the event far past the retention window up front.
    old = datetime(2020, 1, 1, tzinfo=UTC).isoformat()
    async with ctx.storage._client.engine.begin() as conn:
        from sqlalchemy import text

        await conn.execute(text("UPDATE memory_events SET ts = :ts"), {"ts": old})

    runner = make_runner()
    # No projector has applied the event yet -> prune must keep it regardless of age.
    stats = await runner.run("event_log_prune", ctx)
    assert stats == {"status": "ok", "pruned": 0}

    await ctx.storage.set_offset("records", 1)
    stats = await runner.run("event_log_prune", ctx)
    assert stats == {"status": "ok", "pruned": 1}

    full_ctx = await make_ctx(mode="full")
    assert (await runner.run("event_log_prune", full_ctx))["status"] == "skipped"


async def test_dead_letter_logs_and_returns_error_without_raising() -> None:
    runner = InlineRunner()

    async def explode(_ctx: PipelineContext) -> dict[str, object]:
        raise RuntimeError("kaboom")

    runner.register("explode", explode)
    ctx = await make_ctx()
    stats = await runner.run("explode", ctx)
    assert stats["status"] == "error" and "kaboom" in str(stats["error"])
    assert (await runner.run("unknown", ctx))["status"] == "error"
