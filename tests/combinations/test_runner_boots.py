"""C6 §6: background-runner boots (D-16).

* ``inline`` (default) starts and flushes on exit;
* the Temporal seam starts after registration and closes on startup failure;
  Taskiq constructs when its extra is installed, or names the extra when absent;
* these checks need neither Redis nor a Temporal server actually running;
* an unknown runner name is a config error.
"""

from __future__ import annotations

import importlib.util
import sys
from types import ModuleType

import pytest

from memspine.exceptions import ConfigError, MissingServiceError


async def test_inline_runner_boots_and_flushes_on_exit(make_engine) -> None:
    engine = make_engine(template="core", workers={"runner": "inline"})
    await engine.start()
    try:
        assert engine.describe()["runner"] == "inline"
        # A sleep cycle runs the pipelines inline and returns per-stage stats.
        stats = await engine.sleep()
        assert isinstance(stats, dict) and stats
    finally:
        await engine.stop()  # flush-on-exit: the runner closes cleanly


async def test_temporal_runner_starts_after_pipeline_registration(monkeypatch, make_engine) -> None:
    captured: dict[str, object] = {}

    class FakeTemporalRunner:
        def __init__(self, **kwargs: object) -> None:
            captured.update(kwargs)
            self.registered: set[str] = set()
            self.started = False
            self.closed = False

        def register(self, name, pipeline) -> None:
            self.registered.add(name)

        async def start(self) -> None:
            assert self.registered
            self.started = True

        async def run(self, name, ctx) -> dict[str, object]:
            return {"status": "ok"}

        async def close(self) -> None:
            self.closed = True

    module = ModuleType("memspine.workers.temporal_runner")
    module.TemporalRunner = FakeTemporalRunner  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "memspine.workers.temporal_runner", module)
    engine = make_engine(
        template="core",
        workers={"runner": "temporal", "temporal": {"deployment_id": "test-deployment"}},
    )
    await engine.start()
    runner = engine._runner
    try:
        assert engine.describe()["runner"] == "temporal"
        assert runner is not None and runner.started
        assert captured["task_queue"] == "memspine-pipelines-v1-test-deployment"
        assert captured["deployment_id"] == "test-deployment"
        assert captured["journal_path"] == "./memspine-resonate.sqlite"
        assert captured["tls"] is False and captured["api_key"] is None
        assert captured["resonate_task_timeout_s"] == 270.0
        assert captured["step_timeout_s"] == 240.0
        assert captured["step_lease_s"] == 360.0
        assert captured["journal_busy_timeout_s"] == 5.0
        assert captured["resonate_max_concurrent_tasks"] == 4
        assert captured["max_result_bytes"] == 65536
    finally:
        await engine.stop()
    assert runner is not None and runner.closed


async def test_temporal_start_failure_closes_runner_and_engine_dependencies(
    monkeypatch, make_engine
) -> None:
    state = {"closed": False}

    class FailingTemporalRunner:
        def __init__(self, **kwargs: object) -> None:
            pass

        def register(self, name, pipeline) -> None:
            return None

        async def start(self) -> None:
            raise RuntimeError("temporal unavailable")

        async def run(self, name, ctx) -> dict[str, object]:
            return {}

        async def close(self) -> None:
            state["closed"] = True

    module = ModuleType("memspine.workers.temporal_runner")
    module.TemporalRunner = FailingTemporalRunner  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "memspine.workers.temporal_runner", module)
    engine = make_engine(
        template="core",
        workers={"runner": "temporal", "temporal": {"deployment_id": "failing-start"}},
    )
    with pytest.raises(RuntimeError, match="temporal unavailable"):
        await engine.start()
    assert state["closed"]
    assert not engine._started


@pytest.mark.parametrize(
    ("runner", "modules", "extra"),
    [
        ("taskiq", ("taskiq", "taskiq_redis"), "taskiq"),
    ],
)
async def test_optional_runner_constructs_or_names_its_extra(
    runner: str, modules: tuple[str, ...], extra: str, make_engine
) -> None:
    installed = all(importlib.util.find_spec(m) is not None for m in modules)
    engine = make_engine(template="core", workers={"runner": runner})
    if installed:  # pragma: no cover - env-dependent
        await engine.start()
        try:
            assert engine.describe()["runner"] == runner
        finally:
            await engine.stop()
    else:
        with pytest.raises(MissingServiceError) as excinfo:
            await engine.start()
        assert excinfo.value.extra == extra
        assert f"memspine[{extra}]" in str(excinfo.value)


async def test_unknown_runner_is_a_config_error(make_engine) -> None:
    engine = make_engine(template="core", workers={"runner": "bogus"})
    with pytest.raises(ConfigError, match=r"workers\.runner"):
        await engine.start()
