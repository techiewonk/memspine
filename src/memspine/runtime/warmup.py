"""Warm shared resources after Engine start (ADR-079 decision 3, RM-74).

``runtime.shared_resources.warmup`` picks the mode:

- ``lazy``: nothing is warmed; every target reports ``not_requested``.
- ``background`` (default): ``start()`` returns; an Engine-owned task warms the included
  targets, at most ``warmup_concurrency`` at once, within ``warmup_timeout_s`` overall.
  Failures and the timeout are logged and reported per target, never raised.
- ``blocking``: the same pass runs before ``start()`` returns; any failure or the timeout
  raises :class:`WarmupError` naming the categories.

A request that needs a target that is not warm yet just uses it: the shared build in the
resource registry coalesces with the warm-up's (no deadlock), and a warm-up cancelled by
the timeout or by ``stop()`` hands the build to the waiting request instead of failing it.
Targets are created only for configured resources, so nothing optional is imported for a
resource the configuration does not use.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from memspine.exceptions import MemspineError
from memspine.observability.logging import get_logger

__all__ = [
    "WarmState",
    "WarmTarget",
    "WarmupController",
    "WarmupError",
    "find_warm_hook",
]

WarmStatus = Literal["warm", "warming", "failed", "not_requested"]
WarmupMode = Literal["lazy", "background", "blocking"]
_log = get_logger(__name__)


class WarmupError(MemspineError):
    """``warmup: blocking`` could not warm every included resource in time."""


@dataclass(frozen=True, slots=True)
class WarmState:
    """Warm state of one resource for readiness: ``error_type`` is set only when ``failed``
    (the exception class name, ``TimeoutError`` for the overall deadline)."""

    category: str
    status: WarmStatus
    error_type: str | None = None


@dataclass(frozen=True, slots=True)
class WarmTarget:
    """One resource to warm. ``name`` is unique per Engine (``rerank:onnx``);
    ``category`` is one of ``runtime.shared_resources.warmup_include``."""

    name: str
    category: str
    warm: Callable[[], Awaitable[None]]


def find_warm_hook(service: object, depth: int = 4) -> Callable[[], Awaitable[None]] | None:
    """The ``warm()`` of a service or of the adapter it decorates (``_inner`` chain: the
    scheduler's batching wrapper, the per-Engine result cache)."""
    current: Any = service
    for _ in range(depth):
        if current is None:
            return None
        hook = getattr(current, "warm", None)
        if callable(hook):
            return hook  # type: ignore[no-any-return]
        current = getattr(current, "_inner", None)
    return None


class WarmupController:
    """Runs one Engine's warm-up and records the per-target state."""

    def __init__(
        self,
        targets: Collection[WarmTarget],
        *,
        mode: WarmupMode,
        categories: Collection[str],
        timeout_s: float,
        concurrency: int,
    ) -> None:
        if timeout_s <= 0 or concurrency < 1:
            raise ValueError("warm-up needs a positive timeout and concurrency")
        names = [t.name for t in targets]
        if len(set(names)) != len(names):
            raise ValueError("warm-up target names must be unique")
        self._mode = mode
        self._timeout_s = timeout_s
        self._concurrency = concurrency
        self._targets = list(targets)
        self._selected = (
            [] if mode == "lazy" else [t for t in self._targets if t.category in categories]
        )
        chosen = {t.name for t in self._selected}
        self._states: dict[str, WarmState] = {
            t.name: WarmState(t.category, "warming" if t.name in chosen else "not_requested")
            for t in self._targets
        }
        self._task: asyncio.Task[None] | None = None
        self.elapsed_s: float | None = None

    @property
    def mode(self) -> WarmupMode:
        return self._mode

    def states(self) -> dict[str, WarmState]:
        return dict(self._states)

    @property
    def done(self) -> bool:
        return all(s.status != "warming" for s in self._states.values())

    async def run(self) -> None:
        """Warm the selected targets once; records every outcome, raises nothing but
        cancellation (which first cancels and drains its per-target tasks)."""
        if not self._selected:
            self.elapsed_s = 0.0
            return
        started = time.monotonic()
        gate = asyncio.Semaphore(self._concurrency)

        async def one(target: WarmTarget) -> None:
            async with gate:
                try:
                    await target.warm()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._states[target.name] = WarmState(
                        target.category, "failed", type(exc).__name__
                    )
                    _log.warning(
                        "provider.warmup_failed",
                        resource=target.name,
                        category=target.category,
                        error=type(exc).__name__,
                    )
                    return
                self._states[target.name] = WarmState(target.category, "warm")

        tasks = {
            asyncio.create_task(one(t), name=f"memspine-warm-{t.name}"): t for t in self._selected
        }
        timed_out = False
        try:
            _done, pending = await asyncio.wait(tasks, timeout=self._timeout_s)
            timed_out = bool(pending)
        finally:
            leftover = [task for task in tasks if not task.done()]
            for task in leftover:
                task.cancel()
            if leftover:
                await asyncio.gather(*leftover, return_exceptions=True)
            for target in tasks.values():
                if self._states[target.name].status == "warming":
                    reason = "TimeoutError" if timed_out else "CancelledError"
                    self._states[target.name] = WarmState(target.category, "failed", reason)
            self.elapsed_s = time.monotonic() - started
        if pending:
            _log.warning(
                "provider.warmup_timeout",
                timeout_s=self._timeout_s,
                resources=sorted(tasks[t].name for t in pending),
            )
        else:
            _log.debug("provider.warmup_done", elapsed_s=round(self.elapsed_s or 0.0, 3))

    async def run_blocking(self) -> None:
        """``blocking`` mode: warm, then raise naming every category that did not warm."""
        await self.run()
        failed = {name: state for name, state in self._states.items() if state.status == "failed"}
        if failed:
            categories = sorted({state.category for state in failed.values()})
            detail = ", ".join(
                f"{name} ({state.error_type})" for name, state in sorted(failed.items())
            )
            raise WarmupError(
                f"warm-up failed for {', '.join(categories)}: {detail} "
                "(runtime.shared_resources.warmup=blocking; use background or lazy to start "
                "without them)"
            )

    def start_background(self) -> None:
        """``background`` mode: run in an Engine-owned task (``cancel`` stops it)."""
        if self._task is None and self._selected:
            self._task = asyncio.create_task(self.run(), name="memspine-warmup")
            self._task.add_done_callback(_consume)

    async def wait(self, timeout_s: float | None = None) -> Mapping[str, WarmState]:
        """Wait for a background pass (no-op otherwise); returns the states."""
        task = self._task
        if task is not None and not task.done():
            await asyncio.wait({task}, timeout=timeout_s)
        return self.states()

    async def cancel(self) -> None:
        """Stop a pending background pass and drain it (no orphan task, no hang)."""
        task, self._task = self._task, None
        if task is None:
            return
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        for name, state in self._states.items():
            if state.status == "warming":
                self._states[name] = WarmState(state.category, "failed", "CancelledError")


def _consume(task: asyncio.Task[None]) -> None:
    if not task.cancelled():
        task.exception()  # outcomes are already recorded per target
