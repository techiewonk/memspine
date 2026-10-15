"""I73: opt-in per-step write-path timers (monotonic clock, zero cost when off)."""

from __future__ import annotations

import asyncio

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.observability.timers import StepTimers, timed


def _engine(timers: bool) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        observability={"write_timers": timers},
    )


def test_off_by_default() -> None:
    assert MemspineConfig().observability.write_timers is False


def test_step_timers_aggregate_count_total_and_percentiles() -> None:
    timers = StepTimers()
    for ms in range(1, 101):
        timers.record("embed", ms / 1000.0)
    snap = timers.snapshot()
    assert snap["embed"]["count"] == 100
    assert snap["embed"]["total_ms"] == pytest.approx(5050.0)
    assert snap["embed"]["p50_ms"] == pytest.approx(50.0)
    assert snap["embed"]["p95_ms"] == pytest.approx(95.0)
    assert timers.snapshot(reset=True) and timers.snapshot() == {}


async def test_timed_wraps_sync_async_and_raising_steps() -> None:
    timers = StepTimers()

    async def slow() -> int:
        await asyncio.sleep(0.01)
        return 7

    def boom() -> None:
        raise ValueError("x")

    assert await timed(timers, "a", slow)() == 7
    assert timed(timers, "s", lambda: 3)() == 3
    with pytest.raises(ValueError):
        timed(timers, "b", boom)()
    snap = timers.snapshot()
    assert snap["a"]["total_ms"] >= 9.0 and snap["s"]["count"] == 1 and snap["b"]["count"] == 1


async def test_off_wraps_nothing_and_reports_empty() -> None:
    eng = _engine(False)
    await eng.start()
    try:
        await eng.write("Anna lives in Lisbon", namespace="a", entity="anna", attribute="city")
        assert eng.write_timers() == {}
        assert "write_timers" not in eng.describe()
        assert "write" not in eng.__dict__  # the class method, not a wrapper
        assert not hasattr(eng._embedder.embed, "__wrapped__")
    finally:
        await eng.stop()


async def test_on_times_the_steps_and_stop_restores_the_methods() -> None:
    eng = _engine(True)
    await eng.start()
    try:
        await eng.write("Anna lives in Lisbon", namespace="a", entity="anna", attribute="city")
        await eng.write("Anna lives in Porto", namespace="a", entity="anna", attribute="city")
        await eng.write("we met on Monday", namespace="a", memory_type="episodic")
        snap = eng.write_timers()
        assert snap["write_total"]["count"] == 3
        for step in (
            "validation",
            "firewall",
            "redaction",
            "firewall_assess",
            "embed",
            "append_and_project",
            "semantic_write",
            "dedup",
            "conflict_ladder",
        ):
            assert snap[step]["count"] >= 1, step
        assert any(step.startswith("project:") for step in snap)
        assert snap["conflict_ladder"]["count"] == 1  # only the second Anna write conflicts
        for stats in snap.values():
            assert stats["p95_ms"] >= stats["p50_ms"] >= 0.0
        assert snap["write_total"]["total_ms"] >= snap["firewall"]["total_ms"]
        assert eng.describe()["write_timers"]["write_total"]["count"] == 3
        assert eng.write_timers(reset=True) and eng.write_timers() == {}
    finally:
        await eng.stop()
    assert "write" not in eng.__dict__ and eng.write_timers() == {}
