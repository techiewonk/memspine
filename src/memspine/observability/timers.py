"""I73: opt-in per-step timers for the write path.

``observability.write_timers: true`` makes the engine wrap the steps of the write door
(validation, firewall, redaction, embedding, projection, dedup, conflict ladder,
perspective / sensitivity tagging and the optional inline LLM steps) with a monotonic
clock. Off (the default) nothing is wrapped, so the write path is untouched.

Steps are inclusive and may nest (``embed`` runs inside ``firewall_assess``, which runs
inside ``firewall``): read each as "wall time spent inside this step", not as a
partition of ``write_total``.
"""

from __future__ import annotations

import functools
import inspect
import math
from collections import deque
from collections.abc import Awaitable, Callable
from time import perf_counter
from typing import Any

__all__ = ["StepTimers", "timed"]


#: Samples kept per step for the percentiles (a sliding window; count and total stay exact).
_WINDOW = 4096


class StepTimers:
    """Per-step ``count`` / ``total`` / ``p50`` / ``p95`` over the monotonic clock."""

    def __init__(self, window: int = _WINDOW) -> None:
        self._window = window
        self._count: dict[str, int] = {}
        self._total: dict[str, float] = {}
        self._samples: dict[str, deque[float]] = {}

    def record(self, step: str, seconds: float) -> None:
        self._count[step] = self._count.get(step, 0) + 1
        self._total[step] = self._total.get(step, 0.0) + seconds
        samples = self._samples.get(step)
        if samples is None:
            samples = self._samples[step] = deque(maxlen=self._window)
        samples.append(seconds)

    def snapshot(self, *, reset: bool = False) -> dict[str, dict[str, float | int]]:
        """``{step: {count, total_ms, mean_ms, p50_ms, p95_ms}}``, steps sorted by name."""
        out: dict[str, dict[str, float | int]] = {}
        for step in sorted(self._count):
            ordered = sorted(self._samples[step])
            count = self._count[step]
            total = self._total[step]
            out[step] = {
                "count": count,
                "total_ms": round(total * 1000.0, 3),
                "mean_ms": round(total * 1000.0 / count, 3),
                "p50_ms": round(_percentile(ordered, 0.50) * 1000.0, 3),
                "p95_ms": round(_percentile(ordered, 0.95) * 1000.0, 3),
            }
        if reset:
            self.reset()
        return out

    def reset(self) -> None:
        self._count.clear()
        self._total.clear()
        self._samples.clear()


def _percentile(ordered: list[float], q: float) -> float:
    """Nearest-rank percentile of an ascending list (0.0 for no samples)."""
    if not ordered:
        return 0.0
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def timed[F: Callable[..., Any]](timers: StepTimers, step: str, fn: F) -> F:
    """``fn`` wrapped so each call is recorded under ``step`` (sync or async).

    The time is recorded in a ``finally``, so a step that raises still counts."""
    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def run_async(*args: Any, **kwargs: Any) -> Any:
            started = perf_counter()
            try:
                return await fn(*args, **kwargs)
            finally:
                timers.record(step, perf_counter() - started)

        return run_async  # type: ignore[return-value]

    @functools.wraps(fn)
    def run(*args: Any, **kwargs: Any) -> Any:
        started = perf_counter()
        try:
            result = fn(*args, **kwargs)
        except BaseException:
            timers.record(step, perf_counter() - started)
            raise
        if inspect.isawaitable(result):  # a plain def returning an awaitable
            return _await_timed(timers, step, started, result)
        timers.record(step, perf_counter() - started)
        return result

    return run  # type: ignore[return-value]


async def _await_timed(
    timers: StepTimers, step: str, started: float, pending: Awaitable[Any]
) -> Any:
    try:
        return await pending
    finally:
        timers.record(step, perf_counter() - started)
