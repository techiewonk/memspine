"""Bounded periodic sweep for explicitly configured projection cleanup."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Awaitable, Callable, Sequence
from contextlib import suppress

from memspine.providers.changes import CleanupSweepReport

CleanupSweep = Callable[[int, str | None], Awaitable[CleanupSweepReport]]


class ProjectionCleanupWorker:
    """Run one bounded startup sweep and repeat at a configured interval.

    The sweep callback is the only execution path; the worker has no target
    clients and cannot delete data itself. Its SQL-backed caller enforces owner,
    expiry, strategy, verification, retention and cross-process fence checks.
    """

    def __init__(
        self,
        *,
        interval_s: float,
        batch_size: int,
        sweep: CleanupSweep,
    ) -> None:
        if isinstance(interval_s, bool) or not math.isfinite(interval_s) or interval_s <= 0:
            raise ValueError("projection cleanup interval must be finite and positive")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or batch_size < 1:
            raise ValueError("projection cleanup batch size must be a positive integer")
        self._interval_s = interval_s
        self._batch_size = batch_size
        self._sweep = sweep
        self._cursor: str | None = None
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._startup_task: asyncio.Task[CleanupSweepReport] | None = None
        self._stop_event = asyncio.Event()
        self._started = False
        self._stopping = False
        self._last_report: CleanupSweepReport | None = None
        self._last_error: str | None = None

    @property
    def last_report(self) -> CleanupSweepReport | None:
        return self._last_report

    @property
    def last_error(self) -> str | None:
        """Sanitized error type from the last failed sweep, never exception text."""
        return self._last_error

    async def start(self) -> CleanupSweepReport:
        """Complete one startup batch before scheduling periodic batches."""
        if self._started:
            raise RuntimeError("projection cleanup worker is already started")
        self._started = True
        self._stopping = False
        self._stop_event.clear()
        startup_task = asyncio.create_task(
            self._run_once(), name="projection-cleanup-startup-sweep"
        )
        self._startup_task = startup_task
        try:
            report = await self._await_shielded(startup_task)
        except BaseException:
            self._stopping = True
            self._started = False
            raise
        finally:
            if self._startup_task is startup_task:
                self._startup_task = None
        if self._stopping:
            self._started = False
            return report
        self._task = asyncio.create_task(self._run(), name="projection-cleanup-worker")
        return report

    async def stop(self) -> None:
        self._stopping = True
        self._stop_event.set()
        tasks = tuple(task for task in (self._startup_task, self._task) if task is not None)
        try:
            if tasks:
                await self._drain(tasks)
        finally:
            self._startup_task = None
            self._task = None
            self._started = False

    async def _run(self) -> None:
        while not self._stopping:
            with suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval_s)
            if self._stopping:
                break
            try:
                await self._run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._last_error = type(exc).__name__

    async def _run_once(self) -> CleanupSweepReport:
        async with self._lock:
            report = await self._sweep(self._batch_size, self._cursor)
            self._cursor = report.next_cursor
            self._last_report = report
            self._last_error = None
            return report

    @staticmethod
    async def _await_shielded(task: asyncio.Task[CleanupSweepReport]) -> CleanupSweepReport:
        """Let a started sweep finish before propagating caller cancellation."""
        cancelled = False
        while True:
            try:
                result = await asyncio.shield(task)
                break
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                cancelled = True
        if cancelled:
            raise asyncio.CancelledError
        return result

    @staticmethod
    async def _drain(
        tasks: Sequence[asyncio.Task[None] | asyncio.Task[CleanupSweepReport]],
    ) -> None:
        """Drain owned work despite caller cancellation, then preserve it."""

        async def collect() -> None:
            await asyncio.gather(*tasks, return_exceptions=True)

        drain_task = asyncio.create_task(collect())
        cancelled = False
        while not drain_task.done():
            try:
                await asyncio.shield(drain_task)
            except asyncio.CancelledError:
                cancelled = True
        drain_task.result()
        if cancelled:
            raise asyncio.CancelledError


__all__ = ["CleanupSweep", "ProjectionCleanupWorker"]
