"""RM-76: per-process warm pools for separate worker processes (ADR-079 decision 4).

Covers the pool (one started Engine per spec per process, shared by every handler, warmed
at worker start), fork safety (nothing started in a parent survives into a child), the
start-up hooks of the taskiq worker, the REST worker app and the MCP subprocess, and one
real spawned worker process.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any

import pytest

from memspine.api import process as process_mod
from memspine.api.process import EngineSpec, ProcessEngines, process_engine, worker_readiness
from memspine.providers import shared
from memspine.providers.shared import ResourceRegistry, shared_registry
from memspine.runtime.warmup import WarmupError
from memspine.services.embedding.hash import HashEmbedding


@pytest.fixture(autouse=True)
def _fresh_process(monkeypatch: pytest.MonkeyPatch) -> ProcessEngines:
    """Each test is a fresh 'process': its own Engine pool and resource registry."""
    pool = ProcessEngines()
    monkeypatch.setattr(process_mod, "_ENGINES", pool)
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())
    return pool


def _spec(db: Path | str = ":memory:", **shared_resources: Any) -> EngineSpec:
    return EngineSpec(
        "core",
        None,
        {
            "storage": {"provider": "sqlite", "settings": {"path": str(db), "busy_timeout_s": 5.0}},
            "embedding": {"provider": "hash", "settings": {}},
            "read": {"hybrid": True},
            "runtime": {"shared_resources": shared_resources},
        },
        dotenv_path=None,
    )


class _Gate:
    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0
        self.error: Exception | None = None

    async def warm(self) -> None:
        self.started += 1
        await self.release.wait()
        if self.error is not None:
            raise self.error


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    g = _Gate()

    async def warm(_self: HashEmbedding) -> None:
        await g.warm()

    monkeypatch.setattr(HashEmbedding, "warm", warm, raising=False)
    return g


# -- one pool per process -------------------------------------------------------------


async def test_handlers_in_one_process_share_one_started_engine(
    tmp_path: Path, gate: _Gate, _fresh_process: ProcessEngines
) -> None:
    gate.release.set()
    spec = _spec(tmp_path / "m.db")
    pool = _fresh_process
    first, second = await asyncio.gather(
        pool.acquire(spec, role="rest-worker"), pool.acquire(spec, role="mcp")
    )
    try:
        assert first is second and first.is_started  # one coalesced start
        assert first._resource_registry is shared_registry()
        report = worker_readiness()
        assert report["pid"] == os.getpid() and report["ready"]
        (engine_report,) = report["engines"]
        assert engine_report["holders"] == 2
        assert engine_report["roles"] == ["mcp", "rest-worker"]
        assert report["inherited_resources"] == 0 and report["shared_resources"] > 0
        await pool.release(spec, role="mcp")
        assert first.is_started  # another handler still holds it
    finally:
        await pool.release(spec, role="rest-worker")
    assert not first.is_started  # the last holder stopped it
    assert worker_readiness()["engines"] == []


async def test_different_specs_get_separate_engines_on_one_registry(
    tmp_path: Path, gate: _Gate
) -> None:
    gate.release.set()
    db = tmp_path / "m.db"
    async with (
        process_engine(_spec(db, warmup="lazy"), role="a") as a,
        process_engine(_spec(db, warmup="background"), role="b") as b,
    ):
        assert a is not b
        assert a._resource_registry is shared_registry() is b._resource_registry
        assert a._sql is not None and b._sql is not None
        assert a._sql.engine is b._sql.engine  # identical resources shared in the process
        assert a._lexical is b._lexical
        assert len(worker_readiness()["engines"]) == 2


async def test_worker_start_runs_the_configured_warmup(tmp_path: Path, gate: _Gate) -> None:
    async with process_engine(_spec(tmp_path / "m.db"), role="taskiq-worker") as eng:
        (report,) = worker_readiness()["engines"]
        assert report["warm"]["embedder"]["status"] == "warming"  # background: not gated
        assert worker_readiness()["ready"]  # warm state never gates readiness
        gate.release.set()
        states = await eng.wait_until_warm(10.0)
        assert {states["embedder"].status, states["lexical"].status} == {"warm"}
        (report,) = worker_readiness()["engines"]
        assert report["warm_done"] and report["warm"]["lexical"]["status"] == "warm"


async def test_blocking_warmup_failure_fails_the_worker_start(
    tmp_path: Path, gate: _Gate, _fresh_process: ProcessEngines
) -> None:
    gate.error = OSError("weights missing")
    gate.release.set()
    spec = _spec(tmp_path / "m.db", warmup="blocking")
    with pytest.raises(WarmupError, match="embedder"):
        await _fresh_process.acquire(spec, role="taskiq-worker")
    report = worker_readiness()
    assert not report["ready"]
    assert report["engines"][0]["start_error"] == "WarmupError"
    assert report["engines"][0]["state"] == "idle"


async def test_engine_restarts_after_its_last_holder_left(tmp_path: Path, gate: _Gate) -> None:
    gate.release.set()
    spec = _spec(tmp_path / "m.db")
    async with process_engine(spec, role="x") as first:
        pass
    async with process_engine(spec, role="x") as second:
        assert second is not first and second.is_started


# -- fork safety ----------------------------------------------------------------------


async def test_fork_reset_drops_started_engines_and_keeps_constructed_ones(
    tmp_path: Path, gate: _Gate, _fresh_process: ProcessEngines
) -> None:
    gate.release.set()
    pool = _fresh_process
    started_spec, idle_spec = _spec(tmp_path / "a.db"), _spec(tmp_path / "b.db")
    started = await pool.acquire(started_spec, role="parent")
    constructed = pool.engine(idle_spec)
    try:
        pool.reset_after_fork()  # what os.register_at_fork runs in a child
        assert started.is_started  # never stopped by the child
        assert pool.engine(idle_spec) is constructed  # holds no handles: reusable
        assert pool.engine(started_spec) is not started  # rebuilt, never reused
        assert worker_readiness()["engines"][0]["state"] == "idle"
    finally:
        await started.stop()


async def test_rest_worker_refuses_an_engine_started_before_fork(
    tmp_path: Path, gate: _Gate, _fresh_process: ProcessEngines
) -> None:
    pytest.importorskip("fastapi")
    from memspine.api.rest import create_worker_app

    gate.release.set()
    spec = _spec(tmp_path / "m.db")
    app = create_worker_app(spec)
    parent = await _fresh_process.acquire(spec, role="parent")  # started in the "parent"
    try:
        _fresh_process.reset_after_fork()
        with pytest.raises(RuntimeError, match="before this worker process forked"):
            async with app.router.lifespan_context(app):
                pass
    finally:
        await parent.stop()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX fork only")
async def test_real_fork_child_inherits_no_engine_or_resource(tmp_path: Path, gate: _Gate) -> None:
    gate.release.set()
    pool = process_mod.process_engines()
    async with process_engine(_spec(tmp_path / "m.db"), role="parent"):
        assert shared_registry().stats()
        # The fork hooks are registered on the module-level singletons; run them by hand
        # on the per-test instances too, as a real child of this pool would.
        pid = os.fork()
        if pid == 0:  # pragma: no cover - child
            pool.reset_after_fork()
            shared_registry().reset_after_fork()
            ok = not pool.readiness()["engines"] and not shared_registry().stats()
            os._exit(0 if ok else 1)
        _, status = await asyncio.to_thread(os.waitpid, pid, 0)
        assert os.waitstatus_to_exitcode(status) == 0


# -- start-up hooks -------------------------------------------------------------------


async def test_rest_worker_app_lifespan_starts_and_reports_readiness(
    tmp_path: Path, gate: _Gate
) -> None:
    pytest.importorskip("fastapi")
    import httpx

    from memspine.api.rest import create_worker_app

    gate.release.set()
    app = create_worker_app(_spec(tmp_path / "m.db"))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://w") as http:
        assert (await http.get("/ready")).status_code == 503  # lifespan not run yet
        async with app.router.lifespan_context(app):
            response = await http.get("/ready")
            assert response.status_code == 200
            body = response.json()
            assert body["pid"] == os.getpid() and body["engines"][0]["roles"] == ["rest-worker"]
            wrote = await http.post("/write", json={"content": "Mira lives in Lisbon."})
            assert wrote.status_code == 200
        assert worker_readiness()["engines"] == []  # shutdown stopped the Engine


async def test_worker_app_factory_reads_the_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    pytest.importorskip("fastapi")
    from memspine.api.rest import worker_app

    cfg = tmp_path / "c.yaml"
    cfg.write_text("embedding:\n  provider: hash\n", encoding="utf-8")
    monkeypatch.setenv("MEMSPINE_TEMPLATE", "core")
    monkeypatch.setenv("MEMSPINE_CONFIG", str(cfg))
    worker_app()
    (entry,) = process_mod.process_engines()._entries.values()
    assert entry.spec.template == "core" and entry.spec.config == str(cfg)
    assert entry.state == "idle"  # nothing starts before the worker's lifespan


async def test_taskiq_worker_hook_starts_the_engine_before_serving(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, gate: _Gate
) -> None:
    from memspine.api import worker

    gate.release.set()
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "storage:\n  provider: sqlite\n  settings:\n"
        f"    path: '{(tmp_path / 'm.db').as_posix()}'\n    busy_timeout_s: 5.0\n"
        "embedding:\n  provider: hash\n",
        encoding="utf-8",
    )
    seen: dict[str, Any] = {}

    async def fake_serve(eng: Any) -> None:
        seen["started"] = eng.is_started
        seen["readiness"] = worker_readiness()
        seen["engine"] = eng

    monkeypatch.setattr(worker, "serve", fake_serve)
    await worker._run("core", str(cfg))
    assert seen["started"] and seen["readiness"]["ready"]
    assert seen["readiness"]["engines"][0]["roles"] == [worker.ROLE]
    assert not seen["engine"].is_started  # the hook stops it when serving ends


# -- a real spawned worker process ----------------------------------------------------


def test_spawned_worker_process_builds_and_warms_its_own_registry(tmp_path: Path) -> None:
    """A fresh process (what uvicorn --workers spawns) builds and warms its own pool."""
    db = (tmp_path / "w.db").as_posix()
    code = textwrap.dedent(
        f"""
        import asyncio, json, os
        from memspine.api.process import EngineSpec, process_engine, worker_readiness

        spec = EngineSpec("core", None, {{
            "storage": {{"provider": "sqlite", "settings": {{"path": "{db}", "busy_timeout_s": 5.0}}}},
            "embedding": {{"provider": "hash", "settings": {{}}}},
            "read": {{"hybrid": True}},
        }}, dotenv_path=None)

        async def main():
            async with process_engine(spec, role="spawned") as eng:
                await eng.wait_until_warm(60)
                print("READY " + json.dumps(worker_readiness()))

        asyncio.run(main())
        """
    )
    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=180, env=env
    )
    assert out.returncode == 0, out.stderr[-2000:]
    line = next(x for x in out.stdout.splitlines() if x.startswith("READY "))
    report = json.loads(line.removeprefix("READY "))
    assert report["pid"] != os.getpid() and report["ready"]
    assert report["engines"][0]["warm"]["lexical"]["status"] == "warm"
    assert report["shared_resources"] > 0 and report["inherited_resources"] == 0
