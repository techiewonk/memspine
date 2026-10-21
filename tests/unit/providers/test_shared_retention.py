"""RM-78: weight retention in the shared resource registry (keep_warm, idle TTL, memory
budget, per-category cap) and cancellation-safe builds for RM-74 warm-up."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from memspine.providers.shared import (
    ResourceRegistry,
    RetentionPolicy,
    resource_key,
)


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class _Rss:
    """A fake RSS meter: each load adds ``size`` MiB between the before/after reads."""

    def __init__(self) -> None:
        self.value = 100.0

    def __call__(self) -> float:
        return self.value


class _Weights:
    def __init__(self, name: str) -> None:
        self.name = name
        self.closed = 0


def _registry(policy: RetentionPolicy | None = None) -> tuple[ResourceRegistry, _Clock, _Rss]:
    clock, rss = _Clock(), _Rss()
    reg = ResourceRegistry(clock=clock, rss_mb=rss)
    if policy is not None:
        reg.add_retention(policy)
    return reg, clock, rss


async def _load(
    reg: ResourceRegistry,
    category: str,
    name: str,
    *,
    rss: _Rss | None = None,
    size: float = 0.0,
    kind: Any = "weights",
    built: list[_Weights] | None = None,
) -> Any:
    async def build() -> _Weights:
        if rss is not None:
            rss.value += size
        weights = _Weights(name)
        if built is not None:
            built.append(weights)
        return weights

    async def close(weights: _Weights) -> None:
        weights.closed += 1

    key = resource_key(category, {"model": name}, scope="thread")
    return await reg.acquire(key, build, close=close, kind=kind)


async def test_without_a_policy_weights_close_on_last_release() -> None:
    reg, _, _ = _registry()
    lease = await _load(reg, "model.embedding", "a")
    weights = lease.value
    await lease.aclose()
    assert weights.closed == 1 and reg.stats() == ()


async def test_keep_warm_retains_weights_at_zero_refs_and_reuses_them() -> None:
    reg, _, _ = _registry(RetentionPolicy(keep_warm=True))
    built: list[_Weights] = []
    lease = await _load(reg, "model.embedding", "a", built=built)
    await lease.aclose()
    assert built[0].closed == 0
    (stat,) = reg.stats()
    assert stat.refs == 0 and stat.idle and stat.kind == "weights"
    again = await _load(reg, "model.embedding", "a", built=built)
    assert len(built) == 1 and again.value is built[0]  # no reload
    assert not reg.stats()[0].idle
    await again.aclose()
    assert await reg.evict_idle() == 1
    assert built[0].closed == 1 and reg.stats() == ()


async def test_keep_warm_never_retains_connections() -> None:
    reg, _, _ = _registry(RetentionPolicy(keep_warm=True))
    lease = await _load(reg, "sql", "db", kind="connection")
    conn = lease.value
    await lease.aclose()
    assert conn.closed == 1 and reg.stats() == ()


async def test_loop_scoped_entries_are_never_retained() -> None:
    reg, _, _ = _registry(RetentionPolicy(keep_warm=True))

    async def build() -> _Weights:
        return _Weights("loop")

    async def close(weights: _Weights) -> None:
        weights.closed += 1

    lease = await reg.acquire(
        resource_key("model.x", {}, scope="loop"), build, close=close, kind="weights"
    )
    weights = lease.value
    await lease.aclose()
    assert weights.closed == 1


async def test_idle_ttl_closes_only_expired_idle_weights() -> None:
    reg, clock, _ = _registry(RetentionPolicy(keep_warm=True, idle_ttl_s=60.0))
    old = await _load(reg, "model.embedding", "old")
    held = await _load(reg, "model.embedding", "held")
    old_weights = old.value
    await old.aclose()
    clock.now += 30
    assert await reg.sweep() == 0  # not expired yet
    clock.now += 31
    assert await reg.sweep() == 1
    assert old_weights.closed == 1
    clock.now += 1000
    assert await reg.sweep() == 0  # the held entry is in use: never evicted
    assert held.value.closed == 0
    await held.aclose()


async def test_idle_ttl_timer_fires_a_sweep() -> None:
    reg, clock, _ = _registry(RetentionPolicy(keep_warm=True, idle_ttl_s=0.01))
    lease = await _load(reg, "model.embedding", "a")
    weights = lease.value
    await lease.aclose()
    clock.now += 1  # the timer's sweep sees the entry as expired
    for _ in range(50):
        if weights.closed:
            break
        await asyncio.sleep(0.01)
    assert weights.closed == 1


async def test_memory_budget_evicts_least_recently_used_idle_first() -> None:
    reg, clock, rss = _registry(RetentionPolicy(keep_warm=True, memory_budget_mb=250))
    a = await _load(reg, "model.embedding", "a", rss=rss, size=100)
    clock.now += 1
    b = await _load(reg, "model.reranker", "b", rss=rss, size=100)
    clock.now += 1
    wa, wb = a.value, b.value
    await a.aclose()  # a: idle, least recently used
    clock.now += 1
    await b.aclose()
    assert {s.size_mb for s in reg.stats()} == {100.0}
    c = await _load(reg, "model.gliner", "c", rss=rss, size=100)  # 300 > 250
    assert wa.closed == 1 and wb.closed == 0 and c.value.closed == 0
    await c.aclose()
    await reg.evict_idle()


async def test_memory_budget_never_evicts_weights_in_use() -> None:
    reg, _, rss = _registry(RetentionPolicy(keep_warm=True, memory_budget_mb=50))
    a = await _load(reg, "model.embedding", "a", rss=rss, size=100)
    b = await _load(reg, "model.embedding", "b", rss=rss, size=100)
    assert a.value.closed == 0 and b.value.closed == 0  # over budget, but both in use
    wa = a.value
    await a.aclose()
    assert wa.closed == 1  # released and over budget: evicted at once
    await b.aclose()


async def test_max_models_per_category_caps_resident_weights() -> None:
    reg, clock, _ = _registry(RetentionPolicy(keep_warm=True, max_models_per_category=1))
    a = await _load(reg, "model.gliner", "a")
    wa = a.value
    await a.aclose()
    clock.now += 1
    other = await _load(reg, "model.embedding", "x")  # another category: no effect
    assert wa.closed == 0
    b = await _load(reg, "model.gliner", "b")
    assert wa.closed == 1  # the idle gliner made room
    await b.aclose()
    await other.aclose()
    assert len(reg.stats()) == 2
    await reg.evict_idle()


async def test_retention_merges_to_the_most_retaining_policy() -> None:
    merged = RetentionPolicy.merge(
        [
            RetentionPolicy(keep_warm=False, idle_ttl_s=1.0),
            RetentionPolicy(keep_warm=True, idle_ttl_s=10.0, memory_budget_mb=100),
            RetentionPolicy(keep_warm=True, idle_ttl_s=20.0, memory_budget_mb=None),
        ]
    )
    assert merged == RetentionPolicy(True, 20.0, None, None)
    assert RetentionPolicy.merge([]) == RetentionPolicy()
    reg, _, _ = _registry()
    token = reg.add_retention(RetentionPolicy(keep_warm=True))
    assert reg.retention.keep_warm
    reg.remove_retention(token)
    reg.remove_retention(token)  # idempotent
    assert not reg.retention.keep_warm


async def test_retained_weights_survive_policy_removal() -> None:
    reg, _, _ = _registry()
    token = reg.add_retention(RetentionPolicy(keep_warm=True))
    lease = await _load(reg, "model.embedding", "a")
    weights = lease.value
    await lease.aclose()
    reg.remove_retention(token)
    assert weights.closed == 0 and reg.stats()[0].idle  # process-life retention
    await reg.evict_idle("model.embedding")
    assert weights.closed == 1


async def test_cancelled_builder_hands_the_build_to_a_waiter() -> None:
    """A warm-up cancelled mid-load (timeout or stop) must not fail a request waiting on
    the same shared build: the waiter builds it itself."""
    reg = ResourceRegistry()
    started = asyncio.Event()
    builds = 0

    async def build() -> str:
        nonlocal builds
        builds += 1
        if builds == 1:
            started.set()
            await asyncio.sleep(10)
        return "weights"

    key = resource_key("model.slow", {}, scope="thread")
    warm = asyncio.create_task(reg.acquire(key, build, kind="weights"))
    await started.wait()
    waiter = asyncio.create_task(reg.acquire(key, build, kind="weights"))
    await asyncio.sleep(0)
    warm.cancel()
    with pytest.raises(asyncio.CancelledError):
        await warm
    lease = await asyncio.wait_for(waiter, 1.0)
    assert lease.value == "weights" and builds == 2
    await lease.aclose()


async def test_cancelled_warm_hands_the_warm_to_a_waiter() -> None:
    reg = ResourceRegistry()
    calls = 0
    started = asyncio.Event()

    async def warm(_: str) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await asyncio.sleep(10)

    async def build() -> str:
        return "w"

    lease = await reg.acquire(resource_key("m", {}, scope="thread"), build, warm=warm)
    first = asyncio.create_task(lease.warm())
    await started.wait()
    second = asyncio.create_task(lease.warm())
    await asyncio.sleep(0)
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    await asyncio.wait_for(second, 1.0)
    assert calls == 2 and reg.stats()[0].warm
    await lease.aclose()
