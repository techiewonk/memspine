"""ADR-079 / RM-71: keyed, refcounted, warmed shared resource registry."""

from __future__ import annotations

import asyncio
import os
import threading
from typing import Any

import pytest

from memspine.providers.shared import ResourceKey, ResourceRegistry, resource_key, shared_registry


class _Res:
    def __init__(self) -> None:
        self.closed = 0
        self.warmed = 0


def _factory(made: list[_Res], delay: float = 0.0) -> Any:
    async def build() -> _Res:
        await asyncio.sleep(delay)
        res = _Res()
        made.append(res)
        return res

    return build


async def _close(res: _Res) -> None:
    res.closed += 1


async def test_identical_keys_share_and_differing_settings_do_not() -> None:
    reg, made = ResourceRegistry(), []
    a = resource_key("sql", {"path": "x.db"}, {"pool": 5}, scope="loop")
    b = resource_key("sql", {"path": "x.db"}, {"pool": 5}, scope="loop")
    c = resource_key("sql", {"path": "x.db"}, {"pool": 6}, scope="loop")
    assert a == b and a != c
    la = await reg.acquire(a, _factory(made), close=_close)
    lb = await reg.acquire(b, _factory(made), close=_close)
    lc = await reg.acquire(c, _factory(made), close=_close)
    assert la.value is lb.value and lc.value is not la.value
    assert len(made) == 2
    for lease in (la, lb, lc):
        await lease.aclose()


async def test_concurrent_first_acquires_build_once() -> None:
    reg, made = ResourceRegistry(), []
    key = resource_key("vector", {"uri": "a"}, scope="loop")
    leases = await asyncio.gather(
        *(reg.acquire(key, _factory(made, 0.01), close=_close) for _ in range(8))
    )
    assert len(made) == 1 and all(lease.value is made[0] for lease in leases)
    for lease in leases:
        await lease.aclose()
    assert made[0].closed == 1


async def test_last_release_closes_exactly_once_and_close_is_idempotent() -> None:
    reg, made = ResourceRegistry(), []
    key = resource_key("graph", {"path": "g"}, scope="thread")
    first = await reg.acquire(key, _factory(made), close=_close)
    second = await reg.acquire(key, _factory(made), close=_close)
    await first.aclose()
    await first.aclose()
    assert made[0].closed == 0  # another holder remains
    await second.aclose()
    assert made[0].closed == 1
    with pytest.raises(RuntimeError, match="released"):
        _ = second.value
    third = await reg.acquire(key, _factory(made), close=_close)
    assert third.value is not made[0]  # a closed resource is never handed out again
    await third.aclose()


async def test_failed_build_is_not_cached() -> None:
    reg, calls = ResourceRegistry(), []

    async def boom() -> _Res:
        calls.append(1)
        raise ConnectionError("down")

    key = resource_key("cache", {"url": "redis://x"}, scope="loop")
    with pytest.raises(ConnectionError):
        await reg.acquire(key, boom)
    made: list[_Res] = []
    lease = await reg.acquire(key, _factory(made))
    assert len(calls) == 1 and len(made) == 1
    await lease.aclose()


async def test_cancelled_waiter_does_not_leak_the_resource() -> None:
    reg, made = ResourceRegistry(), []
    key = resource_key("llm", {"model": "m"}, scope="thread")
    builder = asyncio.create_task(reg.acquire(key, _factory(made, 0.05), close=_close))
    await asyncio.sleep(0)
    waiter = asyncio.create_task(reg.acquire(key, _factory(made), close=_close))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    lease = await builder
    await lease.aclose()
    assert made[0].closed == 1 and reg.stats() == ()


async def test_warm_runs_once_and_failed_warm_can_retry() -> None:
    reg, made, attempts = ResourceRegistry(), [], []

    async def warm(res: _Res) -> None:
        attempts.append(1)
        await asyncio.sleep(0.01)
        if len(attempts) == 1:
            raise TimeoutError("first warm fails")
        res.warmed += 1

    key = resource_key("embedder", {"model": "e"}, scope="thread")
    lease = await reg.acquire(key, _factory(made), warm=warm)
    with pytest.raises(TimeoutError):
        await lease.warm()
    assert reg.stats()[0].warm is False and reg.stats()[0].warm_error == "TimeoutError"
    other = await reg.acquire(key, _factory(made), warm=warm)
    await asyncio.gather(lease.warm(), other.warm(), lease.warm())
    assert made[0].warmed == 1 and len(attempts) == 2 and reg.stats()[0].warm is True
    await lease.aclose()
    await other.aclose()


def test_loop_scoped_keys_differ_per_event_loop() -> None:
    async def key() -> ResourceKey:
        return resource_key("sql", {"path": "x"}, scope="loop")

    first, second = asyncio.run(key()), None
    result: list[ResourceKey] = []
    t = threading.Thread(target=lambda: result.append(asyncio.run(key())))
    t.start()
    t.join()
    second = result[0]
    assert first.identity == second.identity and first.scope != second.scope


def test_entries_of_a_closed_loop_are_not_reused() -> None:
    reg, made = ResourceRegistry(), []

    async def acquire_and_keep() -> ResourceKey:
        key = resource_key("sql", {"path": "x"}, scope="loop")
        await reg.acquire(key, _factory(made))
        return key

    loop = asyncio.new_event_loop()
    key = loop.run_until_complete(acquire_and_keep())
    loop.close()

    async def again() -> Any:
        return await reg.acquire(key, _factory(made))

    loop2 = asyncio.new_event_loop()
    try:
        lease = loop2.run_until_complete(again())
        assert lease.value is made[1]
    finally:
        loop2.close()


async def test_key_from_another_process_is_rejected() -> None:
    key = resource_key("sql", {"path": "x"}, scope="thread")
    foreign = ResourceKey(key.category, key.identity, key.scope, os.getpid() + 1)
    with pytest.raises(RuntimeError, match="another process"):
        await ResourceRegistry().acquire(foreign, _factory([]))


async def test_reset_after_fork_forgets_without_closing() -> None:
    reg, made = ResourceRegistry(), []
    key = resource_key("graph", {"path": "g"}, scope="thread")
    await reg.acquire(key, _factory(made), close=_close)
    reg.reset_after_fork()
    assert reg.stats() == () and made[0].closed == 0


def test_secrets_are_hashed_not_stored() -> None:
    key = resource_key("weaviate", {"url": "http://w"}, {"auth_digest": "s3cr3t"}, scope="thread")
    assert "s3cr3t" not in repr(key) and len(key.identity) == 32


def test_process_registry_is_a_singleton() -> None:
    assert shared_registry() is shared_registry()
