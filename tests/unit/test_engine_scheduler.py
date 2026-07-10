"""D1: the engine wires the autonomous sleep scheduler (workers.sleep_interval_seconds)."""

from __future__ import annotations

import asyncio

from memspine.engine import Engine


async def _engine(interval: float | None) -> Engine:
    workers = {"sleep_interval_seconds": interval} if interval is not None else {}
    engine = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        workers=workers,
    )
    await engine.start()
    return engine


async def test_scheduler_off_by_default() -> None:
    engine = await _engine(None)
    try:
        assert engine.describe()["scheduler"] is False
        assert engine._scheduler is None
    finally:
        await engine.stop()


async def test_scheduler_runs_the_cycle_on_its_interval() -> None:
    engine = await _engine(0.01)
    try:
        assert engine.describe()["scheduler"] is True
        # The loop drives consolidate/decay/prune without an explicit sleep():
        # count sleep cycles by wrapping the runner-facing pipeline context.
        seen = 0
        original = engine._pipeline_ctx

        def counting_ctx():  # type: ignore[no-untyped-def]
            nonlocal seen
            seen += 1
            return original()

        engine._pipeline_ctx = counting_ctx  # type: ignore[method-assign]
        await asyncio.sleep(0.15)
        assert seen > 0, "the scheduler should have run at least one sleep cycle"
    finally:
        await engine.stop()
    # Stopped cleanly: the background task is gone.
    assert engine._scheduler is None


async def test_stop_cancels_the_scheduler() -> None:
    engine = await _engine(0.01)
    scheduler = engine._scheduler
    assert scheduler is not None and scheduler.running
    await engine.stop()
    assert not scheduler.running
