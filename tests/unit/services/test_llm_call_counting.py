"""R5-6: the counting wrapper (``_Counted``) and ``Engine.model_calls()`` via a real router."""

from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.services.llm.base import LLMRouter, LLMService, _Counted


class _Stub:
    """A picklable stub provider that returns a canned reply per call."""

    def __init__(self, name: str, reply: str) -> None:
        self.name = name
        self.reply = reply
        self.calls = 0

    @property
    def provider_id(self) -> str:
        return f"stub:{self.name}"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        return self.reply


def test_counted_deepcopies_pickles_and_reprs() -> None:
    counted = LLMRouter({"extract": _Stub("x", "{}")}).for_role("extract")
    clone = copy.deepcopy(counted)
    assert clone.provider_id == "stub:x"
    restored = pickle.loads(pickle.dumps(counted))
    assert restored.provider_id == "stub:x"
    assert "extract" in repr(counted) and "_Stub" in repr(counted)


def test_counted_rejects_dunders_and_inner_lookups() -> None:
    counted = _Counted(_Stub("x", "{}"), "extract", {})
    with pytest.raises(AttributeError):
        _ = counted.__deepcopy__  # type: ignore[attr-defined]
    bare = _Counted.__new__(_Counted)  # what copy/pickle build: no __init__
    with pytest.raises(AttributeError):
        _ = bare._inner
    assert counted.name == "x"  # ordinary attributes still delegate


async def test_router_counts_per_role() -> None:
    router = LLMRouter({"extract": _Stub("e", "{}"), "judge": _Stub("j", "{}")})
    for _ in range(2):
        await router.for_role("extract").chat([])
    await router.for_role("judge").chat([])
    assert router.call_counts() == {"extract": 2, "judge": 1}


async def test_model_calls_after_write_read_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    stubs: dict[str, LLMService] = {
        "extract": _Stub("extract", '{"facts": []}'),
        "query_rewrite": _Stub("query_rewrite", "1. Ana morning runs\n2. Ana jogging"),
        "reflect": _Stub("reflect", '{"insights": []}'),
    }

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"consolidation": {"mine_facts": True, "reflect_profile": True}},
            },
            "semantic": {"enabled": True, "policies": {"entity_extraction": "llm"}},
            "reflective": {"enabled": True},
        },
        read={"hybrid": False, "compose_rewrites": True},
    )
    await eng.start()
    try:
        assert eng.model_calls() == {}
        await eng.write("Ana lives in Lyon", namespace="a", memory_type="semantic")
        after_write = eng.model_calls()
        assert after_write == {"extract": 1}

        t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
        msgs = [
            {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
            for i, c in enumerate(
                ["Ana: I run every morning", "Ana: no coffee after noon", "Bob: nice routine"]
            )
        ]
        await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
        await eng.read("How often does Ana run?", namespace="a", mode="compose", top_k=2)
        after_read = eng.model_calls()
        assert after_read["query_rewrite"] == 1
        assert after_read.get("extract", 0) == after_write["extract"]

        await eng.sleep()
        after_sleep = eng.model_calls()
        assert after_sleep["reflect"] == 1  # one reflected session
        assert after_sleep["extract"] == after_read["extract"] + 1  # one mined session
        assert after_sleep["query_rewrite"] == 1
        # the counts match what the stubs actually saw
        assert after_sleep == {
            role: stub.calls  # type: ignore[attr-defined]
            for role, stub in stubs.items()
            if stub.calls  # type: ignore[attr-defined]
        }
    finally:
        await eng.stop()
