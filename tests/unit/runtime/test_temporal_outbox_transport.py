"""The Engine-facing transport dispatches real durable scopes to its bound Activity."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

import pytest

from memspine.clients.sqlite.sync import sqlite_sync_engine
from memspine.runtime.outbox import SqlJobStore
from memspine.runtime.temporal_outbox import (
    OUTBOX_DRAIN_ACTIVITY,
    InvalidDrainRequest,
    TemporalOutboxRunner,
    drain_request,
)
from memspine.schemas.durable.common import OwnerScope

requires_temporal_sdk = pytest.mark.skipif(
    importlib.util.find_spec("temporalio") is None,
    reason="workflow dispatch requires memspine[temporal]",
)


class _Handle:
    def __init__(self, run: Callable[[], Any]) -> None:
        self._run = run

    async def result(self) -> dict[str, Any]:
        return cast(dict[str, Any], await self._run())


class _Client:
    def __init__(self, worker: _Worker) -> None:
        self.worker = worker
        self.calls: list[tuple[str, str]] = []
        self.after_last_step: Callable[[dict[str, Any]], Any] | None = None

    async def start_workflow(self, name: str, request: dict[str, Any], **kw: Any) -> _Handle:
        assert name == "memspine.outbox.drain"
        assert kw["id"].startswith("memspine-outbox-")
        self.calls.append((request["owner"], request["stage"]))

        async def run() -> dict[str, Any]:
            activity = self.worker.activities[OUTBOX_DRAIN_ACTIVITY].__self__
            totals: dict[str, Any] = {"steps": 0, "items": 0, "holes": 0}
            for _ in range(100):
                step = await activity.drain(request)
                totals["steps"] += 1
                totals["items"] += step["items"]
                totals["holes"] += step["holes"]
                if not step["more"]:
                    if self.after_last_step is not None:
                        callback, self.after_last_step = self.after_last_step, None
                        await callback(request)
                    return totals
            raise AssertionError("fake workflow did not converge")

        return _Handle(run)


class _Worker:
    task_queue = "test-queue"

    def __init__(self) -> None:
        self.activities: dict[str, Any] = {}
        self.client = _Client(self)

    def register_activity(self, name: str, fn: Any) -> None:
        assert name not in self.activities
        self.activities[name] = fn


def _runner(db: Path, *, deployment_id: str = "test-deployment") -> TemporalOutboxRunner:
    return TemporalOutboxRunner(
        SqlJobStore(sqlite_sync_engine(str(db), busy_timeout_s=10.0)),
        deployment_id=deployment_id,
        config_fingerprint=hashlib.sha256(str(db.resolve()).encode()).hexdigest(),
        activity_timeout_s=30,
        retry_max_attempts=2,
    )


@requires_temporal_sdk
async def test_dispatches_discovery_before_new_window_and_reopened_scopes(
    tmp_path: Path,
) -> None:
    db = tmp_path / "outbox.sqlite"
    old = OwnerScope(tenant="default", owner="unseen")
    new = OwnerScope(tenant="default", owner="current")
    produced: list[str] = []
    runner = _runner(db)

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return payload

    async def discovery_commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        produced.append(f"discovery:{scope.owner}")
        await runner.enqueue(scope, "window", {"value": payload["value"]})

    async def window_commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        produced.append(f"window:{scope.owner}")

    runner.register("window.discovery", prepare=prepare, commit=discovery_commit)
    runner.register("window", prepare=prepare, commit=window_commit)
    worker = _Worker()
    runner.bind(worker)
    await runner.enqueue(old, "window.discovery", {"value": 1})
    await runner.enqueue(new, "window", {"value": 2})
    report = await runner.start()
    assert report.items == 3
    assert produced == ["discovery:unseen", "window:current", "window:unseen"]
    assert worker.client.calls == [
        ("unseen", "window.discovery"),
        ("current", "window"),
        ("unseen", "window"),
    ]
    assert (await runner.drain()).items == 0
    runner.close()


@requires_temporal_sdk
async def test_rekicks_new_work_arriving_as_workflow_completes(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "outbox.sqlite")
    scope = OwnerScope(tenant="default", owner="same")
    committed: list[int] = []

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        committed.append(offset)

    runner.register("window", prepare=prepare, commit=commit)
    worker = _Worker()
    runner.bind(worker)
    await runner.enqueue(scope, "window", {"n": 1})

    async def late_item(request: dict[str, Any]) -> None:
        await runner.enqueue(scope, "window", {"n": 2})

    worker.client.after_last_step = late_item
    report = await runner.drain(scope)
    assert report.items == 2
    assert committed == [0, 1]
    assert worker.client.calls == [("same", "window"), ("same", "window")]
    runner.close()


async def test_unbound_transport_never_drains_inline(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "outbox.sqlite")
    with pytest.raises(RuntimeError, match="started worker"):
        await runner.drain()
    runner.close()


async def test_stop_waits_for_inflight_activity_before_store_close(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "outbox.sqlite")
    scope = OwnerScope(tenant="default", owner="active")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        entered.set()
        await release.wait()
        return payload

    async def commit(scope: OwnerScope, offset: int, payload: Any) -> None:
        pass

    runner.register("window", prepare=prepare, commit=commit)
    worker = _Worker()
    runner.bind(worker)
    await runner.enqueue(scope, "window", {"n": 1})
    activity = worker.activities[OUTBOX_DRAIN_ACTIVITY].__self__
    active = asyncio.create_task(activity.drain(runner.request(scope, "window")))
    stopped: asyncio.Task[Any] | None = None
    try:
        await asyncio.wait_for(entered.wait(), 2)
        stopped = asyncio.create_task(runner.stop(0.5))
        await asyncio.sleep(0.05)
        assert not stopped.done(), "stop returned while the Activity still uses the outbox store"
    finally:
        release.set()
        await active
        if stopped is not None:
            await stopped
        runner.close()


async def test_stop_waits_for_sql_thread_after_caller_cancel(tmp_path: Path) -> None:
    runner = _runner(tmp_path / "outbox.sqlite")
    entered = threading.Event()
    release = threading.Event()

    def blocked_io() -> None:
        entered.set()
        release.wait()

    caller = asyncio.create_task(runner._io(blocked_io))
    stopped: asyncio.Task[Any] | None = None
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        stopped = asyncio.create_task(runner.stop(0))
        await asyncio.sleep(0.05)
        assert not stopped.done(), "stop returned while cancelled SQL work still ran"
    finally:
        release.set()
        if stopped is not None:
            await stopped
        runner.close()


async def test_outbox_activity_rejects_work_for_a_different_backend(tmp_path: Path) -> None:
    """Workers sharing a task queue must not apply another backend's drain request."""
    intended = _runner(tmp_path / "intended.sqlite")
    wrong = _runner(tmp_path / "wrong.sqlite")
    scope = OwnerScope(tenant="default", owner="shared")
    wrong_commits: list[int] = []

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return payload

    async def commit_wrong(scope: OwnerScope, offset: int, payload: Any) -> None:
        wrong_commits.append(offset)

    intended.register("window", prepare=prepare, commit=commit_wrong)
    wrong.register("window", prepare=prepare, commit=commit_wrong)
    await intended.enqueue(scope, "window", {"backend": "intended"})
    await wrong.enqueue(scope, "window", {"backend": "wrong"})
    worker = _Worker()
    wrong.bind(worker)
    try:
        activity = worker.activities[OUTBOX_DRAIN_ACTIVITY].__self__
        for request in (
            intended.request(scope, "window"),
            drain_request(scope, "window"),
            {**wrong.request(scope, "window"), "deployment_id": "different-deployment"},
            {**wrong.request(scope, "window"), "config_fingerprint": "0" * 64},
        ):
            with pytest.raises(InvalidDrainRequest, match="identity mismatch"):
                await activity.drain(request)
        assert wrong_commits == [], "another backend's Activity accepted the intended drain"
        assert (await activity.drain(wrong.request(scope, "window")))["items"] == 1
        assert wrong_commits == [0]
    finally:
        intended.close()
        wrong.close()


@requires_temporal_sdk
async def test_attached_identity_failure_is_nonretryable_temporal_error(tmp_path: Path) -> None:
    from temporalio import testing
    from temporalio.exceptions import ApplicationError

    runner = _runner(tmp_path / "outbox.sqlite")
    worker = _Worker()
    runner.bind(worker)
    activity = worker.activities[OUTBOX_DRAIN_ACTIVITY].__self__
    scope = OwnerScope(tenant="default", owner="shared")
    bad = {**runner.request(scope, "window"), "deployment_id": "other"}
    try:
        with pytest.raises(ApplicationError) as error:
            await testing.ActivityEnvironment().run(activity.temporal_activity, bad)
        assert error.value.type == "InvalidOutboxDrain"
        assert error.value.non_retryable
    finally:
        runner.close()
