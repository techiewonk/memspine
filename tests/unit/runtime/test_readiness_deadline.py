"""Direct outbox readiness deadlines: no Engine, model or external service."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine.runtime.jobs import ManualClock
from memspine.runtime.outbox import SqlJobStore, memory_engine
from memspine.runtime.runner import InlineRunner
from memspine.schemas.durable.common import OwnerScope

SCOPE = OwnerScope(tenant="t", owner="readiness")


def runner() -> InlineRunner:
    # A frozen business clock must not defeat the direct wait's wall deadline.
    return InlineRunner(
        SqlJobStore(memory_engine()), clock=ManualClock(datetime(2026, 10, 21, tzinfo=UTC))
    )


async def test_slow_inline_prepare_times_out_and_durable_item_can_resume() -> None:
    outbox = runner()
    gate = asyncio.Event()
    prepared = asyncio.Event()
    committed: list[int] = []

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        prepared.set()
        await gate.wait()
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        committed.append(offset)

    outbox.register("derive", prepare=prepare, commit=commit)
    try:
        await outbox.enqueue(SCOPE, "derive", {"message": "m1"})
        started = time.monotonic()
        outcome = await asyncio.wait_for(outbox.wait(SCOPE, ["derive"], timeout=0.03), 1)
        assert time.monotonic() - started < 0.5
        assert prepared.is_set()
        assert outcome.status == "partial" and outcome.pending == ("derive",)
        assert outcome.targets == {"derive": 0} and not outcome.holes
        assert committed == []
        assert outbox.pending_items(SCOPE, "derive") == [(0, {"message": "m1"})]
        assert outbox.depth() == 1 and not outbox._drain_lock.locked()
        assert outbox.store.jobs()[0].state == "queued"

        gate.set()
        outcome = await outbox.wait(SCOPE, ["derive"], timeout=1)
        assert outcome.status == "ready" and committed == [0]
        assert outbox.depth() == 0
    finally:
        outbox.close()


async def test_slow_inline_commit_times_out_without_advancing_watermark() -> None:
    outbox = runner()
    gate = asyncio.Event()
    entered = asyncio.Event()
    effects: set[int] = set()

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        # Effects are idempotent: timeout can happen after an effect but before checkpoint.
        effects.add(offset)
        entered.set()
        await gate.wait()

    outbox.register("derive", prepare=prepare, commit=commit)
    try:
        await outbox.enqueue(SCOPE, "derive", {})
        outcome = await asyncio.wait_for(outbox.wait(SCOPE, ["derive"], timeout=0.03), 1)
        assert entered.is_set() and outcome.status == "partial"
        assert outbox.book.progress(SCOPE, "derive").watermark == -1
        assert outbox.store.jobs()[0].state == "queued"
        gate.set()
        assert (await outbox.wait(SCOPE, ["derive"], timeout=1)).status == "ready"
        assert effects == {0} and outbox.book.progress(SCOPE, "derive").watermark == 0
    finally:
        outbox.close()


async def test_wait_expiry_does_not_cancel_an_independent_drain() -> None:
    outbox = runner()
    gate = asyncio.Event()
    entered = asyncio.Event()
    committed: list[int] = []

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        entered.set()
        await gate.wait()
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        committed.append(offset)

    outbox.register("derive", prepare=prepare, commit=commit)
    await outbox.enqueue(SCOPE, "derive", {})
    drain = asyncio.create_task(outbox.drain())
    try:
        await asyncio.wait_for(entered.wait(), 1)
        outcome = await asyncio.wait_for(outbox.wait(SCOPE, ["derive", "graph"], timeout=0.02), 1)
        assert outcome.status == "partial" and outcome.missing == ("derive", "graph")
        assert not drain.done() and not drain.cancelled()
        gate.set()
        await asyncio.wait_for(drain, 1)
        assert committed == [0]
    finally:
        gate.set()
        await asyncio.gather(drain, return_exceptions=True)
        outbox.close()


async def test_caller_cancellation_propagates_and_releases_inline_claim() -> None:
    outbox = runner()
    gate = asyncio.Event()
    entered = asyncio.Event()

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        entered.set()
        await gate.wait()
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        return None

    outbox.register("derive", prepare=prepare, commit=commit)
    await outbox.enqueue(SCOPE, "derive", {})
    task = asyncio.create_task(outbox.wait(SCOPE, ["derive"], timeout=10))
    try:
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert outbox.store.jobs()[0].state == "queued"
        assert outbox.pending_items(SCOPE, "derive") == [(0, {})]
        gate.set()
        assert (await outbox.wait(SCOPE, ["derive"], timeout=1)).status == "ready"
    finally:
        gate.set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        outbox.close()


@pytest.mark.parametrize("wait_budget", [0, -1, timedelta(0), timedelta(seconds=-1)])
async def test_nonpositive_timeout_is_a_readiness_snapshot_without_drain(
    wait_budget: float | timedelta,
) -> None:
    outbox = runner()
    calls: list[int] = []

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        calls.append(offset)
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        return None

    outbox.register("derive", prepare=prepare, commit=commit)
    try:
        await outbox.enqueue(SCOPE, "derive", {})
        outcome = await outbox.wait(SCOPE, ["derive"], timeout=wait_budget)
        assert outcome.status == "partial" and calls == [] and outbox.depth() == 1
    finally:
        outbox.close()


async def test_unrelated_timeout_error_is_not_misreported_as_deadline_expiry() -> None:
    async def failing_sleeper(seconds: float) -> None:
        raise TimeoutError("independent sleeper failure")

    outbox = InlineRunner(SqlJobStore(memory_engine()), sleeper=failing_sleeper)

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        return None

    outbox.register("derive", prepare=prepare, commit=commit)
    try:
        await outbox.enqueue(SCOPE, "derive", {})
        async with outbox._drain_lock:
            with pytest.raises(TimeoutError, match="independent sleeper failure"):
                await outbox.wait(SCOPE, ["derive"], timeout=1)
    finally:
        outbox.close()
