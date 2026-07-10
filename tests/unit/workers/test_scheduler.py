"""D1: autonomous sleep scheduler.

The scheduler is a thin interval loop: it must tick, survive a failing cycle,
never overlap runs, and stop cleanly. Timing is kept tiny and event-driven (an
asyncio.Event the fake cycle sets) so the tests are deterministic, not sleepy.
"""

from __future__ import annotations

import asyncio

import pytest

from memspine.workers.scheduler import SleepScheduler


async def test_ticks_repeatedly() -> None:
    ticks = 0
    reached = asyncio.Event()

    async def cycle() -> object:
        nonlocal ticks
        ticks += 1
        if ticks >= 3:
            reached.set()
        return {}

    sched = SleepScheduler(0.001, cycle)
    sched.start()
    await asyncio.wait_for(reached.wait(), timeout=2.0)
    await sched.stop()
    assert ticks >= 3
    assert not sched.running


async def test_failing_cycle_does_not_kill_the_loop() -> None:
    calls = 0
    reached = asyncio.Event()

    async def cycle() -> object:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")  # first tick blows up
        reached.set()  # loop survived to a second tick
        return {}

    sched = SleepScheduler(0.001, cycle)
    sched.start()
    await asyncio.wait_for(reached.wait(), timeout=2.0)
    await sched.stop()
    assert calls >= 2


async def test_runs_never_overlap() -> None:
    concurrent = 0
    max_seen = 0
    reached = asyncio.Event()

    async def cycle() -> object:
        nonlocal concurrent, max_seen
        concurrent += 1
        max_seen = max(max_seen, concurrent)
        await asyncio.sleep(0.005)  # a slow cycle
        concurrent -= 1
        reached.set()
        return {}

    sched = SleepScheduler(0.001, cycle)
    sched.start()
    await asyncio.wait_for(reached.wait(), timeout=2.0)
    await asyncio.sleep(0.01)
    await sched.stop()
    assert max_seen == 1  # await-then-sleep in one task => no stacking


async def test_stop_is_safe_before_start_and_idempotent() -> None:
    sched = SleepScheduler(1.0, lambda: _noop())
    await sched.stop()  # never started
    sched.start()
    sched.start()  # idempotent
    assert sched.running
    await sched.stop()
    await sched.stop()  # double stop
    assert not sched.running


async def test_rejects_nonpositive_interval() -> None:
    with pytest.raises(ValueError, match="must be positive"):
        SleepScheduler(0, lambda: _noop())


async def _noop() -> object:
    return {}
