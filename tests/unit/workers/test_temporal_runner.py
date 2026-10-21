"""Temporal runner contract tests without a Temporal server or model services."""

from __future__ import annotations

import asyncio
import sys
import types
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine.config.loader import load_config
from memspine.derive.pipeline.stages.context import PipelineContext
from memspine.exceptions import ConfigError
from memspine.workers.temporal_runner import (
    TemporalRunner,
    _configuration_fingerprint,
    _validate_temporal_storage,
    fingerprint_input,
    workflow_id_for_invocation,
)

ApplicationError = pytest.importorskip(
    "temporalio.exceptions", reason="requires [temporal]"
).ApplicationError
pytest.importorskip("resonate", reason="requires [temporal]")


class _Storage:
    async def prune_events(self, older_than: object) -> int:
        return 0

    async def list_namespaces(self) -> list[str]:
        return []

    async def list_records(self, namespace: str, memory_type: str | None = None) -> list[Any]:
        return []

    async def get_record(self, record_id: str) -> None:
        return None

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[Any]:
        return []


class _FakeClient:
    activity_fn: Any = None
    calls: list[dict[str, object]] = []

    @classmethod
    async def connect(cls, address: str, **kwargs: object) -> _FakeClient:
        cls.connect_args = (address, kwargs)
        return cls()

    async def start_workflow(
        self, workflow: str, invocation: dict[str, object], **kwargs: object
    ) -> Any:
        self.__class__.calls.append(invocation)
        self.__class__.last_start_kwargs = kwargs
        result = await _FakeWorker.activity_fn(invocation)

        class Handle:
            async def result(self) -> dict[str, object]:
                return result

        return Handle()


class _FakeWorker:
    activity_fn: Any = None

    def __init__(self, client: object, **kwargs: object) -> None:
        self.activities = kwargs["activities"]
        type(self).activity_fn = self.activities[0]
        self._stopped = asyncio.Event()

    async def run(self) -> None:
        await self._stopped.wait()

    async def shutdown(self) -> None:
        self._stopped.set()


class _FakeResonate:
    def __init__(self, **kwargs: object) -> None:
        self.functions: dict[str, Any] = {}

    def register(self, fn: Any, *, name: str) -> None:
        self.functions[name] = fn

    def start(self) -> None:
        pass

    def options(self, *, timeout: timedelta) -> _FakeResonate:
        self.timeout = timeout
        return self

    def run(self, operation_id: str, name: str) -> Any:
        fn = self.functions[name]

        class Handle:
            async def result(self) -> dict[str, object]:
                async def execute(ctx: object, *_: object) -> dict[str, object]:
                    return await ctx.run(fn)

                return await execute(_FakeResonateContext())

        return Handle()

    async def stop(self) -> None:
        pass


class _FakeResonateContext:
    def run(self, fn: Any, *args: object) -> asyncio.Task[Any]:
        return asyncio.create_task(fn(self, *args))


@pytest.fixture
def fake_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    import temporalio.activity as activity_module
    import temporalio.client as client_module
    import temporalio.worker as worker_module

    resonate_package = types.ModuleType("resonate")
    resonate_package.__path__ = []  # type: ignore[attr-defined]
    resonate_module = types.ModuleType("resonate.resonate")
    retry_module = types.ModuleType("resonate.retry")
    monkeypatch.setitem(sys.modules, "resonate", resonate_package)
    monkeypatch.setitem(sys.modules, "resonate.resonate", resonate_module)
    monkeypatch.setitem(sys.modules, "resonate.retry", retry_module)

    _FakeClient.calls = []
    monkeypatch.setattr(client_module, "Client", _FakeClient)
    monkeypatch.setattr(worker_module, "Worker", _FakeWorker)
    resonate_module.Resonate = _FakeResonate  # type: ignore[attr-defined]
    retry_module.Never = lambda: object()  # type: ignore[attr-defined]
    monkeypatch.setattr(activity_module, "defn", lambda **kwargs: lambda fn: fn)
    monkeypatch.setattr(
        activity_module,
        "info",
        lambda: type(
            "ActivityInfo", (), {"workflow_run_id": "run", "activity_id": "act", "attempt": 1}
        )(),
    )


def _context(config: object | None = None) -> PipelineContext:
    return PipelineContext(
        storage=_Storage(), config=config or load_config(template="base", env={}).config
    )  # type: ignore[arg-type]


def _config(**kwargs: object) -> object:
    return load_config(template="base", overrides=kwargs, env={}).config


def test_temporal_runner_runs_pipeline_inside_activity_and_journals_repeat(
    tmp_path: Path, fake_sdks: None
) -> None:
    calls = 0
    config = _config()
    stable_input = fingerprint_input({"source_revision": "sleep-batch-1"})

    async def pipeline(ctx: PipelineContext) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return {"status": "ok", "profile": ctx.config.profile}

    runner = TemporalRunner(
        address="localhost:7233",
        namespace="test",
        task_queue="test-deployment",
        deployment_id="test-deployment",
        journal_path=tmp_path / "steps.sqlite",
        context_factory=lambda: _context(config),
    )
    runner.register("sleep", pipeline)

    async def exercise() -> None:
        await runner.start()
        first = await runner.run(
            "sleep", _context(config), operation_id="cycle-1", input_fingerprint=stable_input
        )
        second = await runner.run(
            "sleep", _context(config), operation_id="cycle-1", input_fingerprint=stable_input
        )
        assert first == second == {"status": "ok", "profile": "simple"}
        assert calls == 1
        assert len(_FakeClient.calls[0]["operation_id"]) == 64
        assert _FakeClient.calls[0]["invocation_id"] != _FakeClient.calls[1]["invocation_id"]
        assert "config" not in _FakeClient.calls[0]
        assert all(
            not isinstance(value, PipelineContext) for value in _FakeClient.calls[0].values()
        )
        await runner.close()

    asyncio.run(exercise())


def test_same_operation_id_cannot_replay_with_changed_config(
    tmp_path: Path, fake_sdks: None
) -> None:
    configs = [_config(), _config(strict_services=False)]
    stable_input = fingerprint_input({"source_revision": "sleep-batch-1"})
    active_config = [configs[0]]

    async def pipeline(ctx: PipelineContext) -> dict[str, object]:
        return {"profile": ctx.config.profile}

    runner = TemporalRunner(
        address="localhost:7233",
        namespace="test",
        task_queue="queue",
        deployment_id="deployment",
        journal_path=tmp_path / "steps.sqlite",
        context_factory=lambda: _context(active_config[0]),
    )
    runner.register("sleep", pipeline)

    async def exercise() -> None:
        await runner.start()
        await runner.run(
            "sleep",
            _context(configs[0]),
            operation_id="same-operation",
            input_fingerprint=stable_input,
        )
        active_config[0] = configs[1]
        with pytest.raises(ApplicationError, match="different input") as exc_info:
            await runner.run(
                "sleep",
                _context(configs[1]),
                operation_id="same-operation",
                input_fingerprint=stable_input,
            )
        assert exc_info.value.non_retryable
        await runner.close()

    asyncio.run(exercise())


def test_temporal_identity_and_secret_redaction(tmp_path: Path) -> None:
    runner = TemporalRunner(
        address="localhost:7233",
        namespace="prod",
        task_queue="queue",
        deployment_id="prod-deployment",
        journal_path=tmp_path / "steps.sqlite",
        context_factory=lambda: _context(),
        api_key="do-not-show-this",
    )
    assert "do-not-show-this" not in repr(runner)
    assert workflow_id_for_invocation("a", "same") != workflow_id_for_invocation("b", "same")


def test_fingerprint_is_stable_and_timeout_values_are_strict(tmp_path: Path) -> None:
    assert _configuration_fingerprint(_config()) == _configuration_fingerprint(_config())
    with pytest.raises(ConfigError, match="workflow_timeout_s"):
        TemporalRunner(
            address="localhost:7233",
            namespace="test",
            task_queue="queue",
            deployment_id="deployment",
            journal_path=tmp_path / "steps.sqlite",
            context_factory=lambda: _context(),
            workflow_timeout_s=1,
        )


def test_temporal_fingerprint_resolves_sqlite_paths_and_rejects_memory_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = _config()
    first_workdir = tmp_path / "one"
    second_workdir = tmp_path / "two"
    first_workdir.mkdir()
    second_workdir.mkdir()
    monkeypatch.chdir(first_workdir)
    first = _configuration_fingerprint(config)
    monkeypatch.chdir(second_workdir)
    second = _configuration_fingerprint(config)
    assert first != second

    in_memory = config.model_copy(
        update={"storage": config.storage.model_copy(update={"settings": {"path": ":memory:"}})}
    )
    with pytest.raises(ConfigError, match="filesystem-backed SQLite"):
        _validate_temporal_storage(in_memory)
