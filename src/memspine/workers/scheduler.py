"""Autonomous maintenance scheduler (D1, amends D-16).

The v0.1 sleep cycle (consolidate → … → decay → prune) runs only when a caller
invokes ``Engine.sleep()``. That leaves the learning dynamics — decay tier
transitions, consolidation, community reorganization, rolling-log pruning —
dormant unless something drives them. D1 adds an opt-in background loop that
runs the full cycle on a fixed interval so a long-lived engine keeps its memory
healthy without an external cron.

It is deliberately thin: a single asyncio task that sleeps ``interval`` then runs
one cycle, forever, until cancelled. Overlap is impossible (await-then-sleep in
one task), so a slow cycle simply delays the next tick rather than stacking.
Each run is guarded — a failing cycle is logged and the loop continues, never
crashing the engine. Durability of the *work* is the runner's job (inline/dbos/
taskiq); the scheduler only decides *when*, so it stays anti-lock-in (D-17).

Off by default (``workers.sleep_interval_seconds = None``): ``profile="simple"``
and every existing deployment behave exactly as before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable

from memspine.observability.logging import get_logger

__all__ = ["SleepScheduler"]

_log = get_logger(__name__)

#: A single sleep-cycle run. The engine binds this to run_sleep_cycle over a
#: freshly built context, so each tick reads live storage/graph handles.
RunCycle = Callable[[], Awaitable[object]]


class SleepScheduler:
    """Runs a sleep cycle every ``interval_seconds`` in a background task."""

    def __init__(self, interval_seconds: float, run_cycle: RunCycle) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self._interval = interval_seconds
        self._run_cycle = run_cycle
        self._task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.running:
            return  # idempotent: a second start() never spawns a second loop
        self._task = asyncio.create_task(self._loop(), name="memspine-sleep-scheduler")

    async def stop(self) -> None:
        """Cancel the loop and await its exit. Safe to call when never started."""
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        finally:
            self._task = None

    async def _loop(self) -> None:
        _log.info("scheduler.started", interval_seconds=self._interval)
        try:
            while True:
                await asyncio.sleep(self._interval)
                try:
                    await self._run_cycle()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # a bad cycle must never kill the loop
                    _log.warning("scheduler.cycle_failed", error=str(exc), exc_info=True)
        except asyncio.CancelledError:
            _log.info("scheduler.stopped")
            raise
