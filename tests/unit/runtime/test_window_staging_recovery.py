"""Staging survives failed/cancelled outbox acceptance without replaying acknowledgements."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from memspine.core.window import WindowSettings
from memspine.derive.backpressure import BackpressureReceipt, BackpressureRejected
from memspine.workers.windows import WindowPipeline


class Outbox:
    def __init__(self) -> None:
        self.accepted: list[str] = []
        self.on_enqueue: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def register(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def enqueue(self, scope: Any, stage: str, payload: dict[str, Any], **kw: Any) -> None:
        if self.on_enqueue is not None:
            await self.on_enqueue(payload)
        self.accepted.append(payload["id"])


def pipeline(outbox: Outbox) -> WindowPipeline:
    async def extract(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("this test must not run extraction")

    async def get_record(record_id: str) -> None:
        return None

    result = WindowPipeline(
        settings=WindowSettings(),
        sections=("facts",),
        extractor=extract,  # type: ignore[arg-type]  # no extraction in staging tests
        get_record=get_record,
        deposit_fact=None,
        asynchronous=False,
        outbox=outbox,
    )
    result._staged = [("n", {"id": str(i)}) for i in range(3)]
    return result


def staged_ids(value: WindowPipeline) -> list[str]:
    return [payload["id"] for _, payload in value._staged]


@pytest.mark.parametrize("fail_at", [0, 1, 2])
async def test_enqueue_failure_restores_only_unaccepted_suffix_before_new_work(
    fail_at: int,
) -> None:
    outbox = Outbox()
    value = pipeline(outbox)

    async def fail(payload: dict[str, Any]) -> None:
        if payload["id"] == str(fail_at):
            value._staged.append(("n", {"id": "new"}))
            raise RuntimeError("database write refused before acceptance")

    outbox.on_enqueue = fail
    with pytest.raises(RuntimeError, match="database write refused"):
        await value._persist_staged()
    assert outbox.accepted == [str(i) for i in range(fail_at)]
    assert staged_ids(value) == [str(i) for i in range(fail_at, 3)] + ["new"]

    outbox.on_enqueue = None
    await value._persist_staged()
    assert outbox.accepted == ["0", "1", "2", "new"]
    assert value._staged == []


@pytest.mark.parametrize("cancel_at", [0, 1, 2])
async def test_cancellation_settles_acceptance_and_does_not_retry_acknowledged_item(
    cancel_at: int,
) -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(payload: dict[str, Any]) -> None:
        if payload["id"] == str(cancel_at):
            entered.set()
            await release.wait()

    outbox.on_enqueue = pause
    task = asyncio.create_task(value._persist_staged())
    await entered.wait()
    value._staged.append(("n", {"id": "new"}))
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()  # repeated cancellation must not cancel the acceptance task
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert outbox.accepted == [str(i) for i in range(cancel_at + 1)]
    assert staged_ids(value) == [str(i) for i in range(cancel_at + 1, 3)] + ["new"]

    outbox.on_enqueue = None
    await value._persist_staged()
    assert outbox.accepted == ["0", "1", "2", "new"]


async def test_cancellation_during_failing_acceptance_restores_current_item() -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    entered, release = asyncio.Event(), asyncio.Event()

    async def fail_after_pause(payload: dict[str, Any]) -> None:
        entered.set()
        await release.wait()
        raise RuntimeError("provider failed")

    outbox.on_enqueue = fail_after_pause
    task = asyncio.create_task(value._persist_staged())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert staged_ids(value) == ["0", "1", "2"]
    assert outbox.accepted == []


async def test_cancelling_to_thread_acceptance_does_not_duplicate_durable_write() -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    entered, release = asyncio.Event(), threading.Event()
    durable: list[str] = []
    loop = asyncio.get_running_loop()

    def write_in_thread() -> None:
        loop.call_soon_threadsafe(entered.set)
        if not release.wait(5):  # test deadlock guard, not a production enqueue timeout
            raise TimeoutError("test did not release the simulated database write")
        durable.append("0")

    async def accept_in_thread(payload: dict[str, Any]) -> None:
        if payload["id"] == "0":
            await asyncio.to_thread(write_in_thread)

    outbox.on_enqueue = accept_in_thread
    task = asyncio.create_task(value._persist_staged())
    await entered.wait()
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert durable == ["0"] and outbox.accepted == ["0"]
    assert staged_ids(value) == ["1", "2"]
    outbox.on_enqueue = None
    await value._persist_staged()
    assert outbox.accepted == ["0", "1", "2"]


async def test_backpressure_keeps_tail_and_wakes_acknowledged_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    value._async = True
    awakened: list[str] = []
    retries: list[float] = []
    monkeypatch.setattr(value, "_ensure_worker", lambda ns, store: awakened.append(ns))
    monkeypatch.setattr(value, "_retry_persist_later", retries.append)

    async def refuse(payload: dict[str, Any]) -> None:
        if payload["id"] == "1":
            value._staged.append(("n", {"id": "new"}))
            raise BackpressureRejected(BackpressureReceipt("n", "window", "items", 1, 1, 0, 0.25))

    outbox.on_enqueue = refuse
    await value._persist_staged()
    assert outbox.accepted == ["0"]
    assert staged_ids(value) == ["1", "2", "new"]
    assert value._counts["windows_deferred_backpressure"] == 1
    assert awakened == ["n"] and retries == [0.25]


async def test_unexpected_failure_still_wakes_already_acknowledged_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    value._async = True
    awakened: list[str] = []
    monkeypatch.setattr(value, "_ensure_worker", lambda ns, store: awakened.append(ns))

    async def refuse(payload: dict[str, Any]) -> None:
        if payload["id"] == "1":
            raise RuntimeError("provider failed")

    outbox.on_enqueue = refuse
    with pytest.raises(RuntimeError):
        await value._persist_staged()
    assert outbox.accepted == ["0"] and awakened == ["n"]
    assert staged_ids(value) == ["1", "2"]


async def test_concurrent_persist_does_not_overtake_or_duplicate_detached_batch() -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(payload: dict[str, Any]) -> None:
        if payload["id"] == "0":
            entered.set()
            await release.wait()

    outbox.on_enqueue = pause
    first = asyncio.create_task(value._persist_staged())
    await entered.wait()
    value._staged.append(("n", {"id": "new"}))
    second = asyncio.create_task(value._persist_staged())
    await asyncio.sleep(0)
    assert outbox.accepted == []
    release.set()
    await asyncio.gather(first, second)
    assert outbox.accepted == ["0", "1", "2", "new"]
    assert value._staged == []
