"""RM-74: the warm-up controller (lazy / background / blocking, bounds, cancellation)."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from memspine.runtime.warmup import (
    WarmState,
    WarmTarget,
    WarmupController,
    WarmupError,
    find_warm_hook,
)


class _Slow:
    """A fake resource whose warm-up takes ``delay`` seconds (or raises ``error``)."""

    def __init__(self, delay: float = 0.0, error: Exception | None = None) -> None:
        self.delay = delay
        self.error = error
        self.started = 0
        self.finished = 0
        self.cancelled = 0
        self.running = 0
        self.peak = 0

    async def warm(self) -> None:
        self.started += 1
        self.running += 1
        self.peak = max(self.peak, self.running)
        try:
            await asyncio.sleep(self.delay)
            if self.error is not None:
                raise self.error
            self.finished += 1
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        finally:
            self.running -= 1


def _controller(
    targets: dict[str, tuple[str, _Slow]],
    *,
    mode: Any = "background",
    categories: tuple[str, ...] = ("embedder", "reranker", "gliner", "lexical", "llm", "decider"),
    timeout_s: float = 5.0,
    concurrency: int = 2,
) -> WarmupController:
    return WarmupController(
        [WarmTarget(name, category, res.warm) for name, (category, res) in targets.items()],
        mode=mode,
        categories=categories,
        timeout_s=timeout_s,
        concurrency=concurrency,
    )


async def test_lazy_warms_nothing_and_reports_not_requested() -> None:
    res = _Slow()
    ctl = _controller({"embedder": ("embedder", res)}, mode="lazy")
    ctl.start_background()
    await ctl.run_blocking()
    assert res.started == 0
    assert ctl.states() == {"embedder": WarmState("embedder", "not_requested")}


async def test_blocking_waits_for_every_target() -> None:
    a, b = _Slow(0.02), _Slow(0.02)
    ctl = _controller(
        {"embedder": ("embedder", a), "reranker:fastembed": ("reranker", b)}, mode="blocking"
    )
    await ctl.run_blocking()
    assert a.finished == b.finished == 1
    assert {s.status for s in ctl.states().values()} == {"warm"}


async def test_background_returns_at_once_then_becomes_warm() -> None:
    res = _Slow(0.05)
    ctl = _controller({"embedder": ("embedder", res)})
    loop = asyncio.get_running_loop()
    began = loop.time()
    ctl.start_background()
    assert loop.time() - began < 0.04
    assert ctl.states()["embedder"].status == "warming"
    states = await ctl.wait(2.0)
    assert states["embedder"] == WarmState("embedder", "warm") and res.finished == 1


async def test_excluded_categories_are_not_requested() -> None:
    emb, llm = _Slow(), _Slow()
    ctl = _controller(
        {"embedder": ("embedder", emb), "llm:x": ("llm", llm)}, categories=("embedder",)
    )
    await ctl.run()
    assert ctl.states()["llm:x"].status == "not_requested" and llm.started == 0
    assert ctl.states()["embedder"].status == "warm"


async def test_concurrency_is_bounded() -> None:
    slows = {f"t{i}": ("embedder", _Slow(0.02)) for i in range(5)}
    shared_peak: list[int] = []
    running = 0

    def wrap(res: _Slow) -> Any:
        async def warm() -> None:
            nonlocal running
            running += 1
            shared_peak.append(running)
            try:
                await res.warm()
            finally:
                running -= 1

        return warm

    ctl = WarmupController(
        [WarmTarget(n, c, wrap(r)) for n, (c, r) in slows.items()],
        mode="blocking",
        categories=("embedder",),
        timeout_s=5.0,
        concurrency=2,
    )
    await ctl.run_blocking()
    assert max(shared_peak) == 2


async def test_timeout_is_reported_and_drains_its_tasks() -> None:
    fast, slow = _Slow(0.0), _Slow(10.0)
    ctl = _controller({"embedder": ("embedder", fast), "llm:big": ("llm", slow)}, timeout_s=0.05)
    await ctl.run()
    states = ctl.states()
    assert states["embedder"].status == "warm"
    assert states["llm:big"] == WarmState("llm", "failed", "TimeoutError")
    assert slow.cancelled == 1 and slow.running == 0  # no orphan task


async def test_blocking_timeout_raises_naming_the_category() -> None:
    ctl = _controller({"llm:big": ("llm", _Slow(10.0))}, mode="blocking", timeout_s=0.05)
    with pytest.raises(WarmupError, match=r"failed for llm: llm:big \(TimeoutError\)"):
        await ctl.run_blocking()


async def test_blocking_failure_raises_naming_the_category() -> None:
    ctl = _controller(
        {"gliner:gliner_semantic": ("gliner", _Slow(error=OSError("no weights")))},
        mode="blocking",
    )
    with pytest.raises(WarmupError, match="failed for gliner"):
        await ctl.run_blocking()
    assert ctl.states()["gliner:gliner_semantic"] == WarmState("gliner", "failed", "OSError")


async def test_background_failure_is_recorded_not_raised() -> None:
    ctl = _controller({"decider": ("decider", _Slow(error=RuntimeError("boom")))})
    ctl.start_background()
    states = await ctl.wait(2.0)
    assert states["decider"] == WarmState("decider", "failed", "RuntimeError")


async def test_cancel_stops_a_pending_background_warmup() -> None:
    slow = _Slow(10.0)
    ctl = _controller({"embedder": ("embedder", slow)})
    ctl.start_background()
    await asyncio.sleep(0.01)
    assert slow.started == 1
    await asyncio.wait_for(ctl.cancel(), 1.0)  # no hang
    assert slow.cancelled == 1 and slow.running == 0
    assert ctl.states()["embedder"] == WarmState("embedder", "failed", "CancelledError")
    await ctl.cancel()  # idempotent
    tasks = [t for t in asyncio.all_tasks() if t.get_name().startswith("memspine-warm")]
    assert tasks == []


def test_find_warm_hook_walks_decorators() -> None:
    class Inner:
        async def warm(self) -> None:
            return None

    class Wrapper:
        def __init__(self, inner: object) -> None:
            self._inner = inner

    inner = Inner()
    hook = find_warm_hook(Wrapper(Wrapper(inner)))
    assert hook is not None and getattr(hook, "__self__", None) is inner
    assert find_warm_hook(Wrapper(object())) is None
    assert find_warm_hook(None) is None


def test_target_names_must_be_unique() -> None:
    res = _Slow()
    with pytest.raises(ValueError, match="unique"):
        WarmupController(
            [WarmTarget("a", "llm", res.warm), WarmTarget("a", "llm", res.warm)],
            mode="lazy",
            categories=(),
            timeout_s=1.0,
            concurrency=1,
        )
