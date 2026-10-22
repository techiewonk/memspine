# ruff: noqa: ASYNC109
"""W2-06c: ``derive.OnlineExtractor`` over the outbox window stage, stage notes in the window stats,
``read.PendingBarrier`` and the no-model template. Model-free: stub LLMs, hash embedder."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "evals"))
from test_w2_06a_envelope import ItemLLM, _engine
from test_w2_08_grounding import _w2_turns
from test_window_write import FakeWindowLLM, _facts, _send, _turns

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.config.schema.write import WriteConfig
from memspine.derive.online_extractor import OnlineExtractor
from memspine.ingest import windows as ingest_windows
from memspine.read.pending_barrier import PendingBarrier
from memspine.services.llm.base import LLMRouter

# --- config ---------------------------------------------------------------------------------------


def test_flags_default_off() -> None:
    cfg = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            }
        }
    )
    assert cfg.write.online_extractor_v2 is False
    assert cfg.read.wait_for_pending_s == 0.0


# --- stage notes reach the window stats -----------------------------------------------------------


async def test_stage_notes_appear_in_the_window_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    llm = ItemLLM()
    llm.mutations[1] = lambda item: {**item, "text": "Mira said she repotted 99 orchids"}
    eng = await _engine(monkeypatch, llm, grounding_v2=True, online_extractor_v2=True)
    try:
        await _send(eng, "a", "s1", _w2_turns())
        stats = await eng.flush_windows()
        assert stats["online_extractor"]["stages"] == ["grounding"]
        notes = stats["envelope_stages"]
        assert notes["ground.dropped"] == 1 and notes["ground.grounded"] >= 1
        assert eng.window_stats()["envelope_stages"] == notes
        named = ingest_windows.online_extractor(eng)
        assert named.active and named.stage_names == ("grounding",)
    finally:
        await eng.stop()


async def test_flag_off_stats_have_no_new_keys_and_deposit_is_identical(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def run(**write: Any) -> tuple[dict[str, Any], list[str]]:
        eng = await _engine(monkeypatch, ItemLLM(), grounding_v2=True, **write)
        try:
            await _send(eng, "a", "s1", _w2_turns())
            stats = await eng.flush_windows()
            return stats, sorted(f.content for f in await _facts(eng))
        finally:
            await eng.stop()

    off, facts_off = await run()
    on, facts_on = await run(online_extractor_v2=True)
    assert "envelope_stages" not in off and "online_extractor" not in off
    assert facts_off == facts_on
    assert {k: v for k, v in on.items() if k not in ("envelope_stages", "online_extractor")} == off


# --- the no-model template -------------------------------------------------------------------------


async def test_no_model_template_makes_zero_calls_with_every_write_flag_on(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        r = LLMRouter({})
        orig = r.for_role

        def spy(role: str) -> Any:
            calls.append(role)
            return orig(role)

        r.for_role = spy  # type: ignore[method-assign]
        return r

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    flags = {
        n: True
        for n, f in WriteConfig.model_fields.items()
        if f.annotation is bool and f.default is False
    }
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        write={**flags, "window_extract": "facts", "window_async": False, "window_idle_s": 0.0},
    )
    await eng.start()
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))
        assert await eng.flush_windows() == {}
        assert not ingest_windows.online_extractor(eng).active
        assert await eng.search("tea", namespace="a")
        assert calls == []
        assert await _facts(eng) == []
    finally:
        await eng.stop()


async def test_no_model_start_still_fails_with_the_flag_off() -> None:
    from memspine.exceptions import ConfigError

    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        write={"window_extract": "facts"},
    )
    with pytest.raises(ConfigError):
        await eng.start()


# --- the pending barrier ---------------------------------------------------------------------------


async def test_barrier_is_off_by_default_and_waits_nothing() -> None:
    async def never(*_: Any) -> Any:
        raise AssertionError("must not be called")

    out = await PendingBarrier(never).wait("a", 0.0)
    assert out.status == "off"


async def test_barrier_waits_then_returns_ready() -> None:
    async def ready(ns: str, caps: Any, timeout: float) -> Any:
        await asyncio.sleep(0.05)
        return SimpleNamespace(status="ready", missing=())

    out = await PendingBarrier(ready).wait("a", 2.0)
    assert out.status == "ready" and out.waited_ms >= 40


async def test_barrier_times_out_to_partial_and_never_blocks_forever() -> None:
    async def hang(ns: str, caps: Any, timeout: float) -> Any:
        await asyncio.sleep(60)

    out = await asyncio.wait_for(PendingBarrier(hang).wait("a", 0.1), 5)
    assert out.status == "partial" and out.missing == ("window",)

    async def partial(ns: str, caps: Any, timeout: float) -> Any:
        return SimpleNamespace(status="partial", missing=("window",))

    assert (await PendingBarrier(partial).wait("a", 0.1)).status == "partial"

    async def boom(ns: str, caps: Any, timeout: float) -> Any:
        raise RuntimeError("x")

    assert (await PendingBarrier(boom).wait("a", 0.1)).status == "partial"


async def test_barrier_without_an_outbox_is_ready() -> None:
    async def none(*_: Any) -> Any:
        return None

    assert (await PendingBarrier(none).wait("a", 1.0)).status == "ready"


async def _outbox_engine(monkeypatch: pytest.MonkeyPatch, path: Path, **read: Any) -> Engine:
    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter({"window_extract": FakeWindowLLM()})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": str(path), "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        write={"window_extract": "facts", "window_async": False, "window_idle_s": 0.0},
        llm={
            "roles": {
                "window_extract": {
                    "provider": "litellm",
                    "settings": {"model": "fake/window", "timeout_seconds": 60.0},
                }
            }
        },
        workers={"outbox": True},
        read=read,
    )
    await eng.start()
    return eng


async def test_search_waits_for_pending_extraction_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _outbox_engine(monkeypatch, tmp_path / "on.db", wait_for_pending_s=30.0)
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))  # batch mode: windows queue in the outbox
        assert await _facts(eng) == []
        await eng.search("trip", namespace="a")  # the barrier drains inline, then reads
        assert len(await _facts(eng)) >= 6
        res = await ingest_windows.wait_for_pending(eng, "a", 5.0)
        assert res.status == "ready"
    finally:
        await eng.stop()


async def test_search_does_not_wait_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = await _outbox_engine(monkeypatch, tmp_path / "off.db")
    try:
        await _send(eng, "a", "s1", _turns("trip", 8))
        await eng.search("trip", namespace="a")
        assert await _facts(eng) == []
        assert (await ingest_windows.wait_for_pending(eng, "a", 0.0)).status == "off"
    finally:
        await eng.stop()


async def test_online_extractor_is_inert_without_a_pipeline() -> None:
    ex = OnlineExtractor(None)
    assert not ex.active and ex.stage_names == () and ex.stats() == {}
    assert await ex.flush() == {}
    assert await ex.readiness("a") is None
