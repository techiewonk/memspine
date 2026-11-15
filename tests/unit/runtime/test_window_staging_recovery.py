"""The in-memory handoff and durable staging stream preserve cut order on interruption."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

import pytest

from memspine.core.window import WindowSettings
from memspine.derive.backpressure import BackpressureReceipt, BackpressureRejected
from memspine.schemas.durable.common import OwnerScope
from memspine.workers.windows import WindowPipeline


class Outbox:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.accepted: list[str] = []
        self.on_stage: Callable[[], Awaitable[None]] | None = None
        self.on_promote: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    def register(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def stage_items(
        self, scope: OwnerScope, stage: str, payloads: Sequence[dict[str, Any]]
    ) -> None:
        if self.on_stage is not None:
            await self.on_stage()
        known = {row["_staging_id"] for row in self.rows}
        for payload in payloads:
            if payload["_staging_id"] not in known:
                self.rows.append(dict(payload))
                known.add(payload["_staging_id"])

    def staged_scopes(self, stage: str) -> list[OwnerScope]:
        return [OwnerScope(tenant="default", owner="n")] if self.rows else []

    def staged_items(self, scope: OwnerScope, stage: str) -> list[tuple[int, dict[str, Any]]]:
        return [(i, row) for i, row in enumerate(self.rows) if "_promoted_offset" not in row]

    async def promote_staged(
        self,
        scope: OwnerScope,
        stage: str,
        offset: int,
        payload: dict[str, Any],
        *,
        force: bool = False,
    ) -> bool:
        if self.on_promote is not None:
            await self.on_promote(payload)
        if "_promoted_offset" in self.rows[offset]:
            return False
        self.rows[offset]["_promoted_offset"] = len(self.accepted)
        self.accepted.append(payload["id"])
        return True


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
    result._staged = [("n", {"id": str(i), "_staging_id": str(i)}) for i in range(3)]
    return result


def waiting(outbox: Outbox) -> list[str]:
    return [row["id"] for row in outbox.rows if "_promoted_offset" not in row]


async def test_failed_stage_write_keeps_memory_batch_for_retry() -> None:
    outbox = Outbox()
    value = pipeline(outbox)

    async def fail() -> None:
        raise RuntimeError("stage write refused")

    outbox.on_stage = fail
    with pytest.raises(RuntimeError, match="stage write refused"):
        await value._persist_staged()
    assert [payload["id"] for _, payload in value._staged] == ["0", "1", "2"]
    assert outbox.rows == []
    outbox.on_stage = None
    await value._persist_staged()
    assert outbox.accepted == ["0", "1", "2"]


@pytest.mark.parametrize("fail_at", [0, 1, 2])
async def test_promotion_failure_keeps_durable_suffix_before_new_work(fail_at: int) -> None:
    outbox = Outbox()
    value = pipeline(outbox)

    async def fail(payload: dict[str, Any]) -> None:
        if payload["id"] == str(fail_at):
            value._staged.append(("n", {"id": "new", "_staging_id": "new"}))
            raise RuntimeError("promotion acknowledgment lost")

    outbox.on_promote = fail
    with pytest.raises(RuntimeError, match="promotion acknowledgment lost"):
        await value._persist_staged()
    assert outbox.accepted == [str(i) for i in range(fail_at)]
    assert waiting(outbox) == [str(i) for i in range(fail_at, 3)]
    outbox.on_promote = None
    await value._persist_staged()
    assert outbox.accepted == ["0", "1", "2", "new"]


async def test_cancelled_promotion_keeps_durable_cut_for_retry() -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause(payload: dict[str, Any]) -> None:
        entered.set()
        await release.wait()

    outbox.on_promote = pause
    task = asyncio.create_task(value._persist_staged())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert value._staged == []
    assert waiting(outbox) == ["0", "1", "2"]
    release.set()
    outbox.on_promote = None
    await value._persist_staged()
    assert outbox.accepted == ["0", "1", "2"]


async def test_backpressure_keeps_durable_tail_and_wakes_accepted_work(
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
            raise BackpressureRejected(BackpressureReceipt("n", "window", "items", 1, 1, 0, 0.25))

    outbox.on_promote = refuse
    await value._persist_staged()
    assert outbox.accepted == ["0"]
    assert waiting(outbox) == ["1", "2"]
    assert value._counts["windows_deferred_backpressure"] == 1
    assert awakened == ["n"] and retries == [0.25]


async def test_concurrent_stage_does_not_overtake_detached_batch() -> None:
    outbox = Outbox()
    value = pipeline(outbox)
    entered, release = asyncio.Event(), asyncio.Event()

    async def pause() -> None:
        entered.set()
        await release.wait()

    outbox.on_stage = pause
    first = asyncio.create_task(value._persist_staged())
    await entered.wait()
    value._staged.append(("n", {"id": "new", "_staging_id": "new"}))
    second = asyncio.create_task(value._persist_staged())
    release.set()
    await asyncio.gather(first, second)
    assert outbox.accepted == ["0", "1", "2", "new"]
    assert value._staged == []
