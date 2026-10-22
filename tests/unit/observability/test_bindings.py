"""RM-75 (ADR-079 decision 5): the model gate, SLO / cost / ops ledgers and the tap are keyed per
Engine configuration. Engines with identical settings share one instance; different settings never
overwrite each other; stopping one Engine leaves the other working."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.economy import ledger as costs
from memspine.economy.slo import installed_ledger
from memspine.observability import bindings, tap
from memspine.ops import ledger as ops
from memspine.services.concurrency import installed_gate, model_slot


@pytest.fixture(autouse=True)
def _clean() -> Any:
    tap.shutdown()
    yield
    tap.shutdown()
    assert not bindings.live()  # every Engine below is stopped
    assert installed_gate() is None and costs.installed() is None and ops.installed() is None
    assert not tap.enabled()


def _engine(path: Path, **overrides: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        **overrides,
    )


def _runtime(**kw: Any) -> dict[str, Any]:
    return {"scheduler_v2": True, **kw}


async def test_different_runtime_settings_keep_their_own_scheduler(tmp_path: Path) -> None:
    a = _engine(tmp_path / "a.db", runtime=_runtime(model_concurrency=1))
    b = _engine(tmp_path / "b.db", runtime=_runtime(model_concurrency=2))
    await a.start()
    await b.start()
    try:
        assert a._sched_v2 is not None and b._sched_v2 is not None
        assert a._sched_v2 is not b._sched_v2
        ga, gb = a._sched_v2.gate, b._sched_v2.gate
        assert ga is not None and gb is not None and (ga.slots, gb.slots) == (1, 2)
        with bindings.bound(a._binding):
            assert installed_gate() is ga and installed_ledger() is a._sched_v2.ledger
            async with model_slot("llm"):
                assert ga.active == 1 and gb.active == 0
        with bindings.bound(b._binding):
            assert installed_gate() is gb and installed_ledger() is b._sched_v2.ledger
        # with two live Engines, unbound code gets neither Engine's gate
        assert installed_gate() is None
        await a.stop()
        with bindings.bound(b._binding):  # the other Engine keeps working
            assert installed_gate() is gb
            async with model_slot("llm"):
                assert gb.active == 1
        assert await b.search("anything", namespace="n") == []
        assert installed_gate() is gb  # the only live Engine again
    finally:
        await a.stop()
        await b.stop()


async def test_identical_runtime_settings_share_one_scheduler(tmp_path: Path) -> None:
    a = _engine(tmp_path / "a.db", runtime=_runtime(model_concurrency=2))
    b = _engine(tmp_path / "b.db", runtime=_runtime(model_concurrency=2))
    await a.start()
    await b.start()
    try:
        shared = a._sched_v2
        assert shared is not None and b._sched_v2 is shared
        await a.stop()
        assert shared.gate is not None and shared._opened  # b still holds it
        with bindings.bound(b._binding):
            assert installed_gate() is shared.gate
    finally:
        await a.stop()
        await b.stop()
    assert not shared._opened  # the last release closed it


async def test_cost_ledgers_follow_their_engine(tmp_path: Path) -> None:
    budget_a = {"ledger_v2": True}
    budget_b = {"ledger_v2": True, "ledger_persist": True}
    a = _engine(tmp_path / "a.db", economy=budget_a)
    b = _engine(tmp_path / "b.db", economy=budget_b)
    same = _engine(tmp_path / "a.db", economy=budget_a)  # same store and settings as a
    await a.start()
    await b.start()
    await same.start()
    try:
        assert a._cost_ledger is not None and b._cost_ledger is not None
        assert a._cost_ledger is not b._cost_ledger
        assert same._cost_ledger is a._cost_ledger
        with bindings.bound(b._binding), costs.cost_scope(namespace="n", stage="s"):
            costs.record_call("generative", model="m", tokens_in=3)
        assert b.cost_report()["model_calls"] == 1 and a.cost_report()["model_calls"] == 0
        await a.stop()
        assert same._cost_ledger is not None
        with bindings.bound(same._binding), costs.cost_scope(namespace="n", stage="s"):
            costs.record_call("generative", model="m")
        assert same.cost_report()["model_calls"] == 1  # still open after a stopped
    finally:
        for eng in (a, b, same):
            await eng.stop()


async def test_ops_ledgers_record_each_engines_own_verbs(tmp_path: Path) -> None:
    obs = {"ops_ledger": True}
    a = _engine(tmp_path / "a.db", observability=obs)
    b = _engine(tmp_path / "b.db", observability=obs)
    await a.start()
    await b.start()
    try:
        assert a._ops_ledger is not b._ops_ledger  # different stores: different ledgers
        await a.search("x", namespace="n")
        await a.search("y", namespace="n")
        await b.search("z", namespace="n")
        assert a.ops_report()["stages"]["read"]["search"]["runs"] == 2
        assert b.ops_report()["stages"]["read"]["search"]["runs"] == 1
    finally:
        await a.stop()
        await b.stop()


async def test_each_engine_traces_into_its_own_tap(tmp_path: Path) -> None:
    a = _engine(tmp_path / "a.db", observability={"tap": {"enabled": True, "sink": "memory"}})
    b = _engine(
        tmp_path / "b.db",
        observability={"tap": {"enabled": True, "sink": "memory", "content": True}},
    )
    same = _engine(tmp_path / "c.db", observability={"tap": {"enabled": True, "sink": "memory"}})
    off = _engine(tmp_path / "d.db")
    for eng in (a, b, same, off):
        await eng.start()
    try:
        assert a._binding.tap is not None and a._binding.tap is not b._binding.tap
        assert same._binding.tap is a._binding.tap  # identical settings share the sink
        assert off._binding.tap is None
        await b.search("only b", namespace="n")
        await off.search("never traced", namespace="n")
        with bindings.bound(a._binding):
            assert tap.memory_traces() == [] and not tap.content_enabled()
        with bindings.bound(off._binding):
            assert not tap.enabled()
        with bindings.bound(b._binding):
            assert tap.content_enabled()
            assert [t["kind"] for t in tap.memory_traces()] == ["read"]
        await b.stop()
        await a.search("now a", namespace="n")  # a keeps tracing after b stopped
        with bindings.bound(a._binding):
            assert tap.enabled() and [t["kind"] for t in tap.memory_traces()] == ["read"]
    finally:
        for eng in (a, b, same, off):
            await eng.stop()


async def test_one_engine_behaves_as_before(tmp_path: Path) -> None:
    eng = _engine(
        tmp_path / "a.db",
        runtime=_runtime(model_concurrency=2),
        economy={"ledger_v2": True},
        observability={"ops_ledger": True, "tap": {"enabled": True, "sink": "memory"}},
    )
    await eng.start()
    try:
        # code outside any Engine call (a thread, a direct pipeline call) sees the only Engine
        assert eng._sched_v2 is not None and installed_gate() is eng._sched_v2.gate
        assert costs.installed() is eng._cost_ledger and ops.installed() is eng._ops_ledger
        assert tap.enabled()
        tap.event("probe.outside", n=1)
        assert any(e["stage"] == "probe.outside" for t in tap.memory_traces() for e in t["events"])
    finally:
        await eng.stop()
