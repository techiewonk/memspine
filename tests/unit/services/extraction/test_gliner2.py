from __future__ import annotations

import asyncio
import gc
import json
import sys
import threading
import time
import weakref
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType

import pytest

from memspine.clients.weights.gliner2 import load_local_gliner as _load_local_gliner
from memspine.clients.weights.gliner2 import validate_snapshot as _validate_snapshot
from memspine.services.extraction.gliner2 import (
    GLiNERModelPool,
    GLiNERPoolClosedError,
    GLiNERPoolFullError,
)


@dataclass(frozen=True)
class Settings:
    model: str = "fixture/model"
    revision: str | None = "rev-a"
    device: str = "cpu"
    local_files_only: bool = True
    tasks: tuple[str, ...] = ("entities", "classification")
    inference_concurrency: int = 1


@dataclass
class MutableSettings:
    model: str = "owner/original"
    revision: str | None = "rev-a"
    device: str = "cpu"
    local_files_only: bool = True
    tasks: tuple[str, ...] = ("entities",)
    inference_concurrency: int = 1


class FakeModel:
    def __init__(self) -> None:
        self.closed = 0
        self.active = 0
        self.max_active = 0
        self.guard = threading.Lock()

    def work(self, duration: float = 0.01) -> int:
        with self.guard:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            time.sleep(duration)
            return 42
        finally:
            with self.guard:
                self.active -= 1

    def close(self) -> None:
        self.closed += 1


@pytest.mark.asyncio
async def test_concurrent_acquires_share_one_load_and_reuse_key() -> None:
    loads = 0
    load_guard = threading.Lock()
    started = threading.Event()
    allow_load = threading.Event()
    model = FakeModel()

    def loader(_: Settings) -> FakeModel:
        nonlocal loads
        with load_guard:
            loads += 1
        started.set()
        assert allow_load.wait(2)
        return model

    pool = GLiNERModelPool(loader=loader)
    tasks = [asyncio.create_task(pool.acquire(Settings())) for _ in range(8)]
    assert await asyncio.to_thread(started.wait, 1)
    await asyncio.sleep(0)
    allow_load.set()
    leases = await asyncio.gather(*tasks)

    reordered = await pool.acquire(Settings(tasks=("classification", "entities")))
    assert loads == 1
    assert all(lease._entry is leases[0]._entry for lease in leases)
    assert reordered._entry is leases[0]._entry

    for lease in (*leases, reordered):
        await lease.aclose()
    await pool.aclose()
    assert model.closed == 1


@pytest.mark.asyncio
async def test_loader_receives_frozen_effective_settings_snapshot() -> None:
    started = threading.Event()
    allow_load = threading.Event()
    seen: list[str] = []

    def loader(settings: MutableSettings) -> str:
        seen.append(settings.model)
        started.set()
        assert allow_load.wait(2)
        return settings.model

    source = MutableSettings()
    pool = GLiNERModelPool(loader=loader)
    acquire = asyncio.create_task(pool.acquire(source))
    assert await asyncio.to_thread(started.wait, 1)
    source.model = "owner/changed"
    source.tasks = ("relations",)
    allow_load.set()
    lease = await acquire

    assert seen == ["owner/original"]
    assert await lease.call(lambda loaded: loaded) == "owner/original"
    await lease.aclose()
    await pool.aclose()


@pytest.mark.asyncio
async def test_loaded_entry_is_reserved_until_concurrent_acquirers_claim_it() -> None:
    inserted = asyncio.Event()
    permit_waiters = asyncio.Event()
    model = FakeModel()

    class PausingPool(GLiNERModelPool):
        async def _load_entry(self, settings, key, slot):
            entry = await super()._load_entry(settings, key, slot)
            inserted.set()
            await permit_waiters.wait()
            return entry

    pool = PausingPool(loader=lambda _: model)
    waiters = [asyncio.create_task(pool.acquire(Settings())) for _ in range(4)]
    await inserted.wait()

    with pytest.raises(GLiNERPoolFullError):
        await pool.acquire(Settings(model="other/model"))
    assert next(iter(pool._entries.values())).reservations == 4

    permit_waiters.set()
    leases = await asyncio.gather(*waiters)
    assert next(iter(pool._entries.values())).reservations == 0
    assert next(iter(pool._entries.values())).references == 4
    for lease in leases:
        await lease.aclose()
    await pool.aclose()


@pytest.mark.asyncio
async def test_distinct_effective_settings_get_distinct_models_and_pool_is_bounded() -> None:
    loaded: list[Settings] = []
    models: list[FakeModel] = []

    def loader(settings: Settings) -> FakeModel:
        loaded.append(settings)
        model = FakeModel()
        models.append(model)
        return model

    pool = GLiNERModelPool(max_models=2, loader=loader)
    first = await pool.acquire(Settings())
    second = await pool.acquire(Settings(revision="rev-b"))
    entered = threading.Event()
    finish = threading.Event()

    def busy(model: FakeModel) -> None:
        entered.set()
        assert finish.wait(2)

    # Both resident checkpoints are running: a third has no evictable slot.
    calls = [asyncio.create_task(lease.call(busy)) for lease in (first, second)]
    assert await asyncio.to_thread(entered.wait, 1)
    await asyncio.sleep(0.02)
    with pytest.raises(GLiNERPoolFullError):
        await pool.acquire(Settings(model="other/model"))
    finish.set()
    await asyncio.gather(*calls)
    assert len(loaded) == 2
    # Idle: the least recently used one is evicted even though it stays leased...
    third = await pool.acquire(Settings(model="other/model"))
    assert models[0].closed == 1
    assert len(loaded) == 3
    # ...and its lease reloads it transparently on the next call.
    assert await first.call(lambda model: model.work(0)) == 42
    assert len(loaded) == 4
    await first.aclose()
    await second.aclose()
    await third.aclose()
    await pool.aclose()
    assert all(model.closed == 1 for model in models)


@pytest.mark.asyncio
async def test_failed_load_is_not_cached_and_close_rejects_new_work() -> None:
    attempts = 0
    model = FakeModel()

    def loader(_: Settings) -> FakeModel:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary local snapshot failure")
        return model

    pool = GLiNERModelPool(loader=loader)
    with pytest.raises(OSError, match="temporary"):
        await pool.acquire(Settings())
    lease = await pool.acquire(Settings())
    assert attempts == 2
    await pool.aclose()
    with pytest.raises(GLiNERPoolClosedError):
        await pool.acquire(Settings())
    with pytest.raises(GLiNERPoolClosedError):
        await lease.call(lambda loaded: loaded.work(0))
    assert model.closed == 1


@pytest.mark.asyncio
async def test_cancelled_sole_loader_waiter_does_not_leak_later_load_failure() -> None:
    started = threading.Event()
    allow_failure = threading.Event()

    def loader(_: Settings) -> FakeModel:
        started.set()
        assert allow_failure.wait(2)
        raise OSError("late loader failure")

    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    contexts: list[dict[str, object]] = []
    loop.set_exception_handler(lambda _loop, context: contexts.append(context))
    pool = GLiNERModelPool(loader=loader)
    acquire = asyncio.create_task(pool.acquire(Settings()))
    assert await asyncio.to_thread(started.wait, 1)
    acquire.cancel()
    with pytest.raises(asyncio.CancelledError):
        await acquire
    allow_failure.set()
    await asyncio.sleep(0.05)
    await pool.aclose()
    loop.set_exception_handler(previous_handler)

    assert not [
        context
        for context in contexts
        if "Task exception was never retrieved" in str(context.get("message", ""))
    ]


@pytest.mark.asyncio
async def test_call_limit_is_bounded_and_callback_runs_off_event_loop() -> None:
    model = FakeModel()
    pool = GLiNERModelPool(loader=lambda _: model)
    lease = await pool.acquire(Settings(inference_concurrency=2))
    results = await asyncio.gather(*(lease.call(lambda loaded: loaded.work()) for _ in range(8)))
    assert results == [42] * 8
    assert model.max_active == 2
    await lease.aclose()
    await pool.aclose()


@pytest.mark.asyncio
async def test_close_drains_inflight_call_before_disposing_model() -> None:
    model = FakeModel()
    entered = threading.Event()
    finish = threading.Event()

    def blocked(_: FakeModel) -> str:
        entered.set()
        assert finish.wait(2)
        return "done"

    pool = GLiNERModelPool(loader=lambda _: model)
    lease = await pool.acquire(Settings())
    call = asyncio.create_task(lease.call(blocked))
    assert await asyncio.to_thread(entered.wait, 1)
    close = asyncio.create_task(pool.aclose())
    await asyncio.sleep(0.02)
    assert not close.done()
    assert model.closed == 0
    finish.set()
    assert await call == "done"
    await close
    assert model.closed == 1


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_dispose_model_while_worker_thread_runs() -> None:
    model = FakeModel()
    entered = threading.Event()
    finish = threading.Event()

    def blocked(_: FakeModel) -> None:
        entered.set()
        assert finish.wait(2)

    pool = GLiNERModelPool(loader=lambda _: model)
    lease = await pool.acquire(Settings())
    call = asyncio.create_task(lease.call(blocked))
    assert await asyncio.to_thread(entered.wait, 1)
    call.cancel()
    await asyncio.sleep(0.02)
    call.cancel()
    close = asyncio.create_task(pool.aclose())
    await asyncio.sleep(0.02)
    assert not close.done()
    assert model.closed == 0
    finish.set()
    with pytest.raises(asyncio.CancelledError):
        await call
    await close
    assert model.closed == 1


@pytest.mark.asyncio
async def test_tasks_and_concurrency_are_lease_limits_not_weight_keys() -> None:
    """RM-73: profiles differing only in tasks/concurrency share one load, even at
    ``max_models=1`` (no thrash), while each keeps its own concurrency bound."""
    models: list[FakeModel] = []

    def load(_: Settings) -> FakeModel:
        model = FakeModel()
        models.append(model)
        return model

    pool = GLiNERModelPool(max_models=1, loader=load)
    narrow = await pool.acquire(Settings(tasks=("entities",), inference_concurrency=1))
    wide = await pool.acquire(Settings(tasks=("classification",), inference_concurrency=3))
    for _ in range(3):  # alternating consumers never reload
        await narrow.call(lambda model: model.work(0))
        await wide.call(lambda model: model.work(0))
    assert wide._entry is narrow._entry
    assert len(models) == 1
    assert narrow._semaphore is not wide._semaphore
    await asyncio.gather(*(narrow.call(lambda model: model.work()) for _ in range(4)))
    assert models[0].max_active == 1
    models[0].max_active = 0
    await asyncio.gather(*(wide.call(lambda model: model.work()) for _ in range(6)))
    assert models[0].max_active == 3
    await narrow.aclose()
    await wide.aclose()
    await pool.aclose()
    assert models[0].closed == 1


@pytest.mark.asyncio
async def test_two_engine_pools_share_weights_and_last_release_frees() -> None:
    """RM-73: two Engines (two pools) with identical weights load once; stopping one never
    frees weights the other still holds."""
    from memspine.providers.shared import ResourceRegistry

    registry = ResourceRegistry()
    models: list[FakeModel] = []

    def load(_: Settings) -> FakeModel:
        model = FakeModel()
        models.append(model)
        return model

    engine_a = GLiNERModelPool(loader=load, registry=registry)
    engine_b = GLiNERModelPool(loader=load, registry=registry)
    a = engine_a.binding(Settings(tasks=("entities",)))
    b = engine_b.binding(Settings(tasks=("classification",), inference_concurrency=2))
    assert await a.call(lambda model: model.work(0)) == 42
    assert await b.call(lambda model: model.work(0)) == 42
    assert len(models) == 1
    await engine_a.aclose()
    assert models[0].closed == 0
    assert await b.call(lambda model: model.work(0)) == 42  # still served
    await engine_b.aclose()
    assert models[0].closed == 1
    assert registry.stats() == ()

    other = GLiNERModelPool(loader=load, registry=registry)
    different = other.binding(Settings(device="cuda"))
    await different.call(lambda model: model.work(0))
    assert len(models) == 2  # different weight settings load separately
    await other.aclose()


@pytest.mark.asyncio
async def test_binding_holds_one_lease_for_the_pool_lifetime_and_retries_failures() -> None:
    attempts = 0
    model = FakeModel()

    def load(_: Settings) -> FakeModel:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("transient")
        return model

    pool = GLiNERModelPool(loader=load)
    binding = pool.binding(Settings())
    with pytest.raises(OSError, match="transient"):
        await binding.call(lambda loaded: loaded.work(0))
    for _ in range(3):
        assert await binding.call(lambda loaded: loaded.work(0)) == 42
    assert attempts == 2
    assert next(iter(pool._entries.values())).references == 1  # one lifetime lease
    await pool.aclose()
    assert model.closed == 1
    with pytest.raises(GLiNERPoolClosedError):
        await binding.call(lambda loaded: loaded.work(0))


@pytest.mark.asyncio
async def test_close_clears_model_reference_even_if_a_lease_is_retained() -> None:
    model = FakeModel()
    ref = weakref.ref(model)
    models = [model]
    pool = GLiNERModelPool(loader=lambda _: models[0])
    lease = await pool.acquire(Settings())
    del model
    models.clear()

    await pool.aclose()
    gc.collect()

    assert lease._entry.model is None
    assert ref() is None


def test_snapshot_validation_requires_local_configs_weights_and_tokenizer(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint"
    (checkpoint / "encoder_config").mkdir(parents=True)
    (checkpoint / "config.json").write_text(json.dumps({"architecture": "boundary"}))
    (checkpoint / "encoder_config" / "config.json").write_text("{}")
    (checkpoint / "model.safetensors").write_bytes(b"test")
    (checkpoint / "tokenizer_config.json").write_text("{}")
    with pytest.raises(RuntimeError, match="tokenizer"):
        _validate_snapshot(checkpoint)

    (checkpoint / "tokenizer.json").write_text("{}")
    _validate_snapshot(checkpoint)


def test_snapshot_validation_rejects_unsupported_architecture(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint"
    (checkpoint / "encoder_config").mkdir(parents=True)
    (checkpoint / "config.json").write_text(json.dumps({"architecture": "mystery"}))
    (checkpoint / "encoder_config" / "config.json").write_text("{}")
    (checkpoint / "model.safetensors").write_bytes(b"test")
    (checkpoint / "tokenizer_config.json").write_text("{}")
    (checkpoint / "tokenizer.json").write_text("{}")
    with pytest.raises(RuntimeError, match="Unsupported GLiNER checkpoint architecture"):
        _validate_snapshot(checkpoint)


def test_local_snapshot_is_loaded_without_hub_resolution(tmp_path: Path, monkeypatch) -> None:
    checkpoint = _complete_checkpoint(tmp_path)
    calls: list[tuple[str, dict[str, object]]] = []

    class AutoExtractor:
        @staticmethod
        def from_pretrained(path: str, **kwargs: object) -> str:
            calls.append((path, kwargs))
            return "loaded"

    gliner = ModuleType("gliner2")
    gliner.AutoExtractor = AutoExtractor
    hub = ModuleType("huggingface_hub")
    hub.snapshot_download = lambda **_: pytest.fail("local path must bypass Hub")
    monkeypatch.setitem(sys.modules, "gliner2", gliner)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    loaded = _load_local_gliner(Settings(model=str(checkpoint), revision=None, device="cpu"))

    assert loaded == "loaded"
    assert calls == [(str(checkpoint), {"map_location": "cpu"})]


def test_hub_resolution_forwards_revision_and_local_only(tmp_path: Path, monkeypatch) -> None:
    checkpoint = _complete_checkpoint(tmp_path)
    hub_calls: list[dict[str, object]] = []
    loader_calls: list[tuple[str, dict[str, object]]] = []

    class AutoExtractor:
        @staticmethod
        def from_pretrained(path: str, **kwargs: object) -> str:
            loader_calls.append((path, kwargs))
            return "loaded"

    gliner = ModuleType("gliner2")
    gliner.AutoExtractor = AutoExtractor
    hub = ModuleType("huggingface_hub")

    def snapshot_download(**kwargs: object) -> str:
        hub_calls.append(kwargs)
        return str(checkpoint)

    hub.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "gliner2", gliner)
    monkeypatch.setitem(sys.modules, "huggingface_hub", hub)

    _load_local_gliner(Settings(model="owner/model", revision="rev-a"))

    assert hub_calls == [{"repo_id": "owner/model", "revision": "rev-a", "local_files_only": True}]
    assert loader_calls == [(str(checkpoint), {"map_location": "cpu"})]


def test_local_snapshot_explicit_revision_must_match_directory_name(
    tmp_path: Path, monkeypatch
) -> None:
    checkpoint = _complete_checkpoint(tmp_path)
    monkeypatch.setitem(sys.modules, "gliner2", ModuleType("gliner2"))
    monkeypatch.setitem(sys.modules, "huggingface_hub", ModuleType("huggingface_hub"))
    with pytest.raises(RuntimeError, match="must match its directory name"):
        _load_local_gliner(Settings(model=str(checkpoint), revision="different-revision"))


@pytest.mark.parametrize("capacity", [True, 0, -1, 1.5])
def test_pool_capacity_must_be_a_positive_integer(capacity: object) -> None:
    with pytest.raises(ValueError, match="max_models"):
        GLiNERModelPool(max_models=capacity)  # type: ignore[arg-type]


def _complete_checkpoint(tmp_path: Path) -> Path:
    checkpoint = tmp_path / "checkpoint"
    (checkpoint / "encoder_config").mkdir(parents=True)
    (checkpoint / "config.json").write_text(json.dumps({"architecture": "boundary"}))
    (checkpoint / "encoder_config" / "config.json").write_text("{}")
    (checkpoint / "model.safetensors").write_bytes(b"test")
    (checkpoint / "tokenizer_config.json").write_text("{}")
    (checkpoint / "tokenizer.json").write_text("{}")
    return checkpoint
