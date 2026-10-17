from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.providers.lifecycle import LifecycleState
from memspine.runtime.container import Neo4jProjection


def _engine(path: Path) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        cache={"provider": "memory", "settings": {}},
        memories={"semantic": {"enabled": True}},
    )


class _Worker:
    def __init__(self, *, fail: bool = False, block: asyncio.Event | None = None) -> None:
        self.fail = fail
        self.block = block
        self.calls = 0

    async def stop(self) -> None:
        self.calls += 1
        if self.block is not None:
            await self.block.wait()
        if self.fail:
            raise RuntimeError("worker shutdown failed")

    async def close(self) -> None:
        self.calls += 1
        if self.fail:
            raise RuntimeError("worker close failed")


@pytest.mark.asyncio
async def test_worker_close_failures_do_not_skip_sql_and_provider_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path / "worker-cleanup.db")
    await engine.start()
    assert engine._provider_resources is not None
    manager = engine._provider_resources
    sql_close_count = 0
    storage_stop_count = 0

    sql_resource = manager._resources["sql"]
    original_sql_close = sql_resource.close

    async def count_sql_close() -> None:
        nonlocal sql_close_count
        sql_close_count += 1
        await original_sql_close()

    sql_resource.close = count_sql_close
    assert engine._storage is not None
    original_storage_stop = engine._storage.stop

    async def count_storage_stop() -> None:
        nonlocal storage_stop_count
        storage_stop_count += 1
        await original_storage_stop()

    monkeypatch.setattr(engine._storage, "stop", count_storage_stop)
    windows = _Worker(fail=True)
    runner = _Worker(fail=True)
    engine._windows = windows  # type: ignore[assignment]
    engine._runner = runner  # type: ignore[assignment]

    with pytest.raises(RuntimeError, match="engine teardown incomplete") as raised:
        await engine.stop()

    assert "window workers" in str(raised.value)
    assert "task runner" in str(raised.value)
    assert windows.calls == 1
    assert runner.calls == 1
    assert storage_stop_count == 1
    assert sql_close_count == 1
    assert manager.states["sql"] is LifecycleState.STOPPED
    assert not engine.is_started
    with pytest.raises(RuntimeError, match="engine teardown incomplete"):
        await engine.stop()
    assert sql_close_count == 1


@pytest.mark.asyncio
async def test_startup_error_is_preserved_when_partial_cleanup_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _engine(tmp_path / "partial-startup-cleanup.db")
    original_build_windows = Engine._build_windows

    def fail_after_services_built(self: Engine, config: Any) -> Any:
        assert self._provider_resources is not None
        sql_resource = self._provider_resources._resources["sql"]

        async def failed_sql_close() -> None:
            raise RuntimeError("native close detail must not replace startup error")

        sql_resource.close = failed_sql_close
        original_build_windows(self, config)
        raise ConfigError("original injected startup failure")

    monkeypatch.setattr(Engine, "_build_windows", fail_after_services_built)

    with pytest.raises(ConfigError, match="original injected startup failure") as raised:
        await engine.start()

    assert any("Startup cleanup was incomplete" in note for note in raised.value.__notes__)
    assert any("sql:RuntimeError" in note for note in raised.value.__notes__), (
        raised.value.__notes__
    )
    assert all("native close detail" not in note for note in raised.value.__notes__)
    assert engine._provider_resources is not None
    assert engine._provider_resources.states["sql"] is LifecycleState.FAILED
    assert not engine.is_started


@pytest.mark.asyncio
async def test_cancelling_stop_waits_for_worker_cleanup_and_preserves_cancellation(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "cancel-stop-cleanup.db")
    await engine.start()
    assert engine._provider_resources is not None
    manager = engine._provider_resources
    sql_close_count = 0
    sql_resource = manager._resources["sql"]
    original_sql_close = sql_resource.close

    async def count_sql_close() -> None:
        nonlocal sql_close_count
        sql_close_count += 1
        await original_sql_close()

    sql_resource.close = count_sql_close
    entered = asyncio.Event()
    release = asyncio.Event()
    worker = _Worker(block=release)

    async def block_stop() -> None:
        entered.set()
        await worker.stop()

    engine._windows = _WorkerWithStop(block_stop)  # type: ignore[assignment]
    stopping = asyncio.create_task(engine.stop())
    await entered.wait()
    stopping.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await stopping

    assert worker.calls == 1
    assert sql_close_count == 1
    assert manager.states["sql"] is LifecycleState.STOPPED
    assert not engine.is_started


class _WorkerWithStop:
    def __init__(self, stop: Any) -> None:
        self.stop = stop


@pytest.mark.asyncio
async def test_neo4j_projection_closes_runner_even_if_drain_raises() -> None:
    calls: list[str] = []

    class Runner:
        async def stop(self, _deadline_s: float) -> None:
            calls.append("stop")
            raise RuntimeError("transport detail")

        def close(self) -> None:
            calls.append("close")

    projection = Neo4jProjection(
        projector=object(), runner=cast(Any, Runner()), store=object()
    )
    with pytest.raises(RuntimeError, match="stop:RuntimeError") as raised:
        await projection.close()

    assert calls == ["stop", "close"]
    assert "transport detail" not in str(raised.value)
