"""SLO ledger (RT-02; P-21 C): foreground read latency and background lag, measured in-process.

Records two series and answers two questions:

- ``record_read(seconds)``: a foreground read's wall time; ``read_p95()`` over a bounded window.
- ``record_lag(seconds)``: time from a background item being queued to being derived (its
  time-to-searchable); ``lag_p95()``.
- ``set_backlog(...)``: size and age of the oldest unfinished item, recomputed by the caller from
  the *persisted* outbox rows, so a restart does not forget it; ``restart_tail_s`` reports the part
  of that age during which no process was running, separately.

Pure: stdlib only. Budgets are optional; ``report()`` says whether each p95 is within its budget.
"""

from __future__ import annotations

import functools
import math
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

__all__ = [
    "SloLedger",
    "SloReport",
    "install_ledger",
    "installed_ledger",
    "percentile",
    "timed_read",
]


def percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (``q`` in 0..1); 0.0 for no data."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


@dataclass(frozen=True)
class SloReport:
    reads: int
    read_p50_s: float
    read_p95_s: float
    lag_samples: int
    lag_p95_s: float
    backlog_items: int
    backlog_age_s: float
    restart_tail_s: float
    rejected: int
    coalesced: int
    read_ok: bool | None
    lag_ok: bool | None


class SloLedger:
    """Bounded in-process ledger; safe to call from threads."""

    def __init__(
        self,
        *,
        window: int = 2048,
        read_p95_budget_s: float | None = None,
        lag_p95_budget_s: float | None = None,
    ) -> None:
        self._reads: deque[float] = deque(maxlen=window)
        self._lags: deque[float] = deque(maxlen=window)
        self._lock = threading.Lock()
        self.read_p95_budget_s = read_p95_budget_s
        self.lag_p95_budget_s = lag_p95_budget_s
        self.rejected = 0
        self.coalesced = 0
        self._backlog_items = 0
        self._backlog_age_s = 0.0
        self._restart_tail_s = 0.0

    def record_read(self, seconds: float) -> None:
        with self._lock:
            self._reads.append(max(0.0, seconds))

    def record_lag(self, seconds: float) -> None:
        with self._lock:
            self._lags.append(max(0.0, seconds))

    def note_rejected(self) -> None:
        with self._lock:
            self.rejected += 1

    def note_coalesced(self) -> None:
        with self._lock:
            self.coalesced += 1

    def set_backlog(self, items: int, oldest_age_s: float, restart_tail_s: float = 0.0) -> None:
        """Backlog size and age, recomputed from the persisted underived markers."""
        with self._lock:
            self._backlog_items = max(0, items)
            self._backlog_age_s = max(0.0, oldest_age_s)
            self._restart_tail_s = max(0.0, restart_tail_s)

    def read_p95(self) -> float:
        with self._lock:
            return percentile(list(self._reads), 0.95)

    def lag_p95(self) -> float:
        with self._lock:
            return percentile(list(self._lags), 0.95)

    def report(self) -> SloReport:
        with self._lock:
            reads, lags = list(self._reads), list(self._lags)
            rp95, lp95 = percentile(reads, 0.95), percentile(lags, 0.95)
            return SloReport(
                reads=len(reads),
                read_p50_s=percentile(reads, 0.5),
                read_p95_s=rp95,
                lag_samples=len(lags),
                lag_p95_s=lp95,
                backlog_items=self._backlog_items,
                backlog_age_s=self._backlog_age_s,
                restart_tail_s=self._restart_tail_s,
                rejected=self.rejected,
                coalesced=self.coalesced,
                read_ok=None if self.read_p95_budget_s is None else rp95 <= self.read_p95_budget_s,
                lag_ok=None if self.lag_p95_budget_s is None else lp95 <= self.lag_p95_budget_s,
            )


# --------------------------------------------------------------------------- read timing hook
_LEDGER: SloLedger | None = None
_IN_READ: ContextVar[bool] = ContextVar("memspine_slo_in_read", default=False)


def install_ledger(ledger: SloLedger | None) -> SloLedger | None:
    """Make ``ledger`` the process-wide read-latency sink (None removes it); returns the old one."""
    global _LEDGER
    previous, _LEDGER = _LEDGER, ledger
    return previous


def installed_ledger() -> SloLedger | None:
    """The process-wide read-latency ledger, if one is installed (T5-06 ops report)."""
    return _LEDGER


def timed_read[F: Callable[..., Awaitable[Any]]](fn: F) -> F:
    """Decorator for the public read verbs: record the outermost call's wall time in the installed
    ledger (a nested read verb is not counted twice). A pass-through when no ledger is installed."""

    @functools.wraps(fn)
    async def inner(*args: Any, **kwargs: Any) -> Any:
        ledger = _LEDGER
        if ledger is None or _IN_READ.get():
            return await fn(*args, **kwargs)
        token = _IN_READ.set(True)
        started = time.perf_counter()
        try:
            return await fn(*args, **kwargs)
        finally:
            _IN_READ.reset(token)
            ledger.record_read(time.perf_counter() - started)

    return inner  # type: ignore[return-value]
