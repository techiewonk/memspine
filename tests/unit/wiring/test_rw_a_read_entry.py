"""RW-A: R3-01 tiers_v2, R3-02 contract_rules, R3-03 router_rules wiring (model-free)."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.read.pipeline import request as rq


async def _engine(**read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )
    await eng.start()
    await eng.write(
        "Alice lives in Munich",
        namespace="a",
        memory_type="semantic",
        entity="alice",
        attribute="city",
    )
    return eng


async def _read(eng: Engine, q: str = "Where does Alice live?", **kw: Any) -> Any:
    return await eng.read(q, namespace="a", mode="retrieve", **kw)


async def test_flags_off_matches_default_and_no_plan() -> None:
    eng = await _engine()
    try:
        base = [r.content for r in (await _read(eng)).context.records]
        assert rq.current_plan() is None
    finally:
        await eng.stop()
    assert base and not any(c.startswith("[contract]") for c in base)


async def test_contract_rules_adds_typed_header() -> None:
    eng = await _engine(contract_rules=True)
    try:
        recs = [r.content for r in (await _read(eng)).context.records]
    finally:
        await eng.stop()
    assert recs[0].startswith("[contract]")
    assert any("Munich" in c for c in recs)


async def test_tiers_v2_normalises_query_and_resets_plan(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[Any] = []
    real = rq.plan_read

    async def spy(*a: Any, **k: Any) -> Any:
        out = await real(*a, **k)
        seen.append(out)
        return out

    monkeypatch.setattr(rq, "plan_read", spy)
    eng = await _engine(tiers_v2=True)
    try:
        await _read(eng, "  Where   does Alice live?  ", viewer="alice")
        assert rq.current_plan() is None  # reset after the read
    finally:
        await eng.stop()
    query, state = seen[0]
    assert query == "Where does Alice live?"
    assert state.tier is not None and state.tier.name == "fast"
    assert state.request is not None and state.request.viewer == "alice"


async def test_router_rules_suppresses_unrouted_optional_legs() -> None:
    eng = await _engine(router_rules=True, session_leg=True)
    try:
        _, state = await rq.plan_read(
            eng, "Where does Alice live?", "a", viewer=None, budget_tokens=2048
        )
        out = await _read(eng)
    finally:
        await eng.stop()
    assert state.route is not None
    assert "vector" in state.route.leg_names()
    assert "episode" in state.legs_off  # a plain lookup asks for no episode leg
    assert any("Munich" in r.content for r in out.context.records)


async def test_layer_state_counts_what_the_store_has() -> None:
    eng = await _engine(router_rules=True)
    try:
        layers = await rq.layer_state(eng, "a", eng._config().read)
    finally:
        await eng.stop()
    assert layers.is_populated("L1") and not layers.is_populated("L5")
    assert not layers.is_populated("L3")  # no graph store configured
