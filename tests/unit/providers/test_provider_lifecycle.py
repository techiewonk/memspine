from __future__ import annotations

import asyncio

import pytest

from memspine.providers.lifecycle import (
    LifecycleState,
    ProviderLifecycleManager,
    ResourceOwnershipError,
)


@pytest.mark.asyncio
async def test_close_order_respects_dependencies_and_is_idempotent() -> None:
    manager = ProviderLifecycleManager(close_timeout_s=1.0)
    closed: list[str] = []

    async def close(name: str) -> None:
        closed.append(name)

    manager.own("sql", lambda: close("sql"))
    manager.own("graph-client", lambda: close("graph"), depends_on=("sql",))
    borrowed = manager.borrow("sql")

    assert borrowed.ownership == "borrowed"
    first = await manager.close_all()
    second = await manager.close_all()

    assert first.complete
    assert first.closed == ("graph-client", "sql")
    assert second is first
    assert closed == ["graph", "sql"]
    assert manager.states == {"sql": LifecycleState.STOPPED, "graph-client": LifecycleState.STOPPED}


def test_duplicate_unknown_and_late_resources_are_rejected() -> None:
    manager = ProviderLifecycleManager(close_timeout_s=1.0)
    closer = lambda: asyncio.sleep(0)
    with pytest.raises(ResourceOwnershipError, match="unknown resource dependencies"):
        manager.own("graph", closer, depends_on=("sql",))
    manager.own("sql", closer)
    with pytest.raises(ResourceOwnershipError, match="already has an owner"):
        manager.own("sql", closer)
    with pytest.raises(ResourceOwnershipError, match="unknown resource"):
        manager.borrow("other")


@pytest.mark.asyncio
async def test_close_continues_after_error_and_timeout() -> None:
    manager = ProviderLifecycleManager(close_timeout_s=0.01)
    closed: list[str] = []

    async def fail() -> None:
        raise RuntimeError("secret-bearing backend detail")

    async def slow() -> None:
        await asyncio.sleep(1)

    async def finish() -> None:
        closed.append("finish")

    manager.own("first", finish)
    manager.own("second", fail)
    manager.own("third", slow)
    report = await manager.close_all()

    assert closed == ["finish"]
    assert {item.resource_id for item in report.failures} == {"second", "third"}
    assert {item.error_type for item in report.failures} == {"RuntimeError", "TimeoutError"}
    assert next(item for item in report.failures if item.resource_id == "third").timed_out
    assert "secret-bearing" not in repr(report)


@pytest.mark.asyncio
async def test_failed_dependent_blocks_sql_close_but_independent_resources_close() -> None:
    manager = ProviderLifecycleManager(close_timeout_s=0.01)
    closed: list[str] = []

    async def close(name: str) -> None:
        closed.append(name)

    async def stuck_driver() -> None:
        await asyncio.sleep(1)

    manager.own("sql", lambda: close("sql"))
    manager.own("driver", stuck_driver, depends_on=("sql",))
    manager.own("independent", lambda: close("independent"))

    report = await manager.close_all()

    assert closed == ["independent"]
    failures = {failure.resource_id: failure for failure in report.failures}
    assert failures["driver"].error_type == "TimeoutError"
    assert failures["sql"].error_type == "DependencyCloseBlocked"
    assert failures["sql"].blocked_by == ("driver",)
    assert manager.states["sql"] == LifecycleState.REGISTERED


@pytest.mark.asyncio
async def test_failed_dependent_blocks_transitive_dependencies() -> None:
    manager = ProviderLifecycleManager(close_timeout_s=1.0)
    closed: list[str] = []

    async def fail() -> None:
        raise RuntimeError("private driver details")

    async def close(name: str) -> None:
        closed.append(name)

    manager.own("sql", lambda: close("sql"))
    manager.own("sync-lease", lambda: close("sync-lease"), depends_on=("sql",))
    manager.own("driver", fail, depends_on=("sync-lease",))
    manager.own("independent", lambda: close("independent"))

    report = await manager.close_all()

    assert closed == ["independent"]
    failures = {failure.resource_id: failure for failure in report.failures}
    assert failures["driver"].error_type == "RuntimeError"
    assert failures["sync-lease"].error_type == "DependencyCloseBlocked"
    assert failures["sync-lease"].blocked_by == ("driver",)
    assert failures["sql"].error_type == "DependencyCloseBlocked"
    assert failures["sql"].blocked_by == ("driver",)
    assert "private driver details" not in repr(report)


@pytest.mark.asyncio
async def test_build_error_is_preserved_when_partial_cleanup_fails() -> None:
    manager = ProviderLifecycleManager(close_timeout_s=1.0)

    async def broken_close() -> None:
        raise OSError("private connection string")

    with pytest.raises(RuntimeError, match="build failed") as caught:
        async with manager.closing_on_error():
            manager.own("partial-driver", broken_close)
            raise RuntimeError("build failed")

    assert any("partial-driver" in note for note in caught.value.__notes__)
    assert "private connection string" not in str(caught.value)


@pytest.mark.asyncio
async def test_cancelling_close_finishes_cleanup_then_preserves_cancellation() -> None:
    manager = ProviderLifecycleManager(close_timeout_s=1.0)
    closed: list[str] = []
    started = asyncio.Event()

    async def close_slowly() -> None:
        started.set()
        await asyncio.sleep(0.02)
        closed.append("resource")

    manager.own("resource", close_slowly)
    task = asyncio.create_task(manager.close_all())
    await started.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed == ["resource"]
    assert manager.states["resource"] == LifecycleState.STOPPED


def test_close_timeout_must_be_finite_positive_and_explicit() -> None:
    for timeout in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValueError):
            ProviderLifecycleManager(close_timeout_s=timeout)
