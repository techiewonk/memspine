"""RM-75 follow-ups (ADR-079 decision 5): with two started Engines, executor threads, direct
private-method calls, REST and MCP entry points still run as their own Engine; seam leases are
released by the lifecycle manager; the inspect switches are per Engine."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.observability import bindings, tap
from memspine.observability import inspect as insp
from memspine.providers.lifecycle import LifecycleState


@pytest.fixture(autouse=True)
def _clean() -> Any:
    tap.shutdown()
    insp.configure(False)
    yield
    tap.shutdown()
    insp.configure(False)
    assert not bindings.live()


def _engine(path: Path, **overrides: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        **overrides,
    )


_TAP: dict[str, Any] = {"tap": {"enabled": True, "sink": "memory"}}


async def _two(tmp_path: Path, *, taps: bool = False) -> tuple[Engine, Engine]:
    """Two Engines on different stores; with ``taps`` each gets its own memory tap (identical
    tap settings would share one sink, by design)."""
    obs_a: dict[str, Any] = _TAP if taps else {}
    obs_b: dict[str, Any] = {"tap": {**_TAP["tap"], "content": True}} if taps else {}
    a = _engine(tmp_path / "a.db", observability=obs_a)
    b = _engine(tmp_path / "b.db", observability=obs_b)
    await a.start()
    await b.start()
    return a, b


def _traces(eng: Engine) -> list[dict[str, Any]]:
    with eng.bound():
        return list(tap.memory_traces())


# --------------------------------------------------------------------------- executor threads
async def test_executor_threads_keep_the_engine_binding(tmp_path: Path) -> None:
    a, b = await _two(tmp_path)
    try:
        assert bindings.current() is None  # two live Engines: unbound code gets neither
        loop = asyncio.get_running_loop()
        with a.bound():
            assert bindings.current() is a._binding
            assert await bindings.run_in_executor(None, bindings.current) is a._binding
            assert await asyncio.to_thread(bindings.current) is a._binding
            # the plain loop method drops the context (why run_in_executor exists)
            assert await loop.run_in_executor(None, bindings.current) is None
        seen: list[Any] = []

        def raw_thread() -> None:
            with b.bound():  # a host thread opts in explicitly
                seen.append(bindings.current())

        thread = threading.Thread(target=raw_thread)
        thread.start()
        thread.join()
        assert seen == [b._binding]
    finally:
        await a.stop()
        await b.stop()


async def test_decider_pool_threads_keep_the_engine_binding(tmp_path: Path) -> None:
    from memspine.models.providers.decision.decider import OpenDeciderDecider

    a, b = await _two(tmp_path)
    decider = OpenDeciderDecider(workers=2)
    decider._nano = object()  # no weights: only the pool hand-off is under test
    try:
        with b.bound():
            assert await decider._run(bindings.current) is b._binding
    finally:
        await decider.aclose()
        await a.stop()
        await b.stop()


async def test_taskiq_worker_serves_as_its_engine(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("taskiq", reason="taskiq extra is not installed")
    from memspine.runtime import worker

    binding = bindings.EngineBinding()
    seen: list[Any] = []

    async def fake_serve(eng: Any, finish: Any, receiver_cls: Any) -> None:
        seen.append(bindings.current())

    monkeypatch.setattr(worker, "_serve", fake_serve)
    await worker.serve(SimpleNamespace(_binding=binding))
    assert seen == [binding]


# --------------------------------------------------------------------------- private methods
async def test_private_engine_methods_run_bound(tmp_path: Path) -> None:
    a, b = await _two(tmp_path)
    try:
        seen: list[Any] = []
        original = a._require_started

        def spy() -> Any:
            seen.append(bindings.current())
            return original()

        a._require_started = spy  # type: ignore[method-assign]
        assert bindings.current() is None
        await a._records("n")  # a direct call, as from api.ops_* / handle / a pipeline ctx
        assert seen == [a._binding]
    finally:
        await a.stop()
        await b.stop()


# --------------------------------------------------------------------------- MCP / tools / REST
async def test_mcp_tool_calls_trace_into_their_engines_tap(tmp_path: Path) -> None:
    from memspine.api.mcp import McpServer
    from memspine.api.tools import AgentMemoryTools, ToolContext

    a, b = await _two(tmp_path, taps=True)
    try:
        server = McpServer(AgentMemoryTools(a, ToolContext(namespace="mcp-a")))
        reply = await server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "memory_search", "arguments": {"query": "tea"}},
            }
        )
        assert reply is not None and "result" in reply
        assert [t["kind"] for t in _traces(a)] == ["read"]
        assert _traces(b) == []
    finally:
        await a.stop()
        await b.stop()


async def test_rest_requests_run_as_the_apps_engine(tmp_path: Path) -> None:
    pytest.importorskip("fastapi")
    import httpx

    from memspine.api.rest import create_app

    a, b = await _two(tmp_path, taps=True)
    try:
        transport = httpx.ASGITransport(app=create_app(b), raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://memspine") as http:
            found = await http.post(
                "/search", json={"query": "tea"}, headers={"X-Memspine-Namespace": "n"}
            )
            assert found.status_code == 200
            assert (await http.get("/describe")).status_code == 200  # a sync route
        assert _traces(a) == []
        assert "read" in [t["kind"] for t in _traces(b)]
    finally:
        await a.stop()
        await b.stop()


async def test_memory_handle_and_tools_bind_their_verbs() -> None:
    from memspine.api.handle import Memory
    from memspine.api.tools import AgentMemoryTools

    assert getattr(Memory.recall, "_memspine_bound", False)
    assert getattr(Memory.audit_action, "_memspine_bound", False)
    assert getattr(AgentMemoryTools.call, "_memspine_bound", False)
    assert getattr(Engine._append_and_project, "_memspine_bound", False)
    assert not getattr(Engine.bound, "_memspine_bound", False)  # sync methods are untouched


# --------------------------------------------------------------------------- leases via lifecycle
async def test_seam_leases_are_released_by_the_lifecycle_manager(tmp_path: Path) -> None:
    flags: dict[str, Any] = {
        "runtime": {"scheduler_v2": True, "model_concurrency": 2},
        "economy": {"ledger_v2": True},
        "observability": {"ops_ledger": True},
    }
    a = _engine(tmp_path / "a.db", **flags)
    b = _engine(tmp_path / "a.db", **flags)  # same settings and store: shares all three
    await a.start()
    await b.start()
    try:
        seams = ("seam.model_scheduler", "seam.cost_ledger", "seam.ops_ledger")
        resources = a._provider_resources
        assert resources is not None
        assert all(resources.states[s] is LifecycleState.REGISTERED for s in seams)
        sched, cost, ops = a._sched_v2, a._cost_ledger, a._ops_ledger
        assert sched is b._sched_v2 and cost is b._cost_ledger and ops is b._ops_ledger
        await a.stop()
        assert all(resources.states[s] is LifecycleState.STOPPED for s in seams)
        assert sched is not None and sched._opened  # b still holds the shared scheduler
        with b.bound():
            from memspine.models.concurrency import installed_gate

            assert installed_gate() is sched.gate
    finally:
        await a.stop()
        await b.stop()
    assert sched is not None and not sched._opened  # the last lease closed it


# --------------------------------------------------------------------------- inspect switches
async def test_inspect_switches_are_per_engine(tmp_path: Path) -> None:
    on = _engine(
        tmp_path / "a.db",
        observability={**_TAP, "inspect": True, "inspect_content": True},
    )
    off = _engine(tmp_path / "b.db", observability=_TAP)
    await on.start()
    await off.start()  # starting another Engine no longer resets the first one's switches
    try:
        with on.bound():
            assert insp.inspect_enabled() and insp.inspect_content()
            assert insp.ic("text") != tap._NO_CONTENT
        with off.bound():
            assert not insp.inspect_enabled() and not insp.inspect_content()
        assert not insp.inspect_enabled()  # unbound with two live Engines: the process default
        with on.bound():
            with insp.sleep_stage("s"), insp.sleep_cycle(None, "n"):
                pass
        assert on._binding.inspect_cycles == {"n": 1} and off._binding.inspect_cycles == {}
    finally:
        await on.stop()
        await off.stop()
