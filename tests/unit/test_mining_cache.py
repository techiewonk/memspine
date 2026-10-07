"""E1 (plan v3.2): ``consolidation.mining_cache`` makes LLM mining deterministic."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.services.llm.base import LLMRouter

REPLY = '{"facts": [{"entity": "Ana", "attribute": "city", "value": "Lyon", "kind": "state"}]}'


class _Stub:
    def __init__(self) -> None:
        self.calls = 0
        self.options: list[dict[str, Any]] = []

    @property
    def provider_id(self) -> str:
        return "stub:extract"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        self.options.append(options)
        return REPLY


async def _miner(monkeypatch: pytest.MonkeyPatch, cached: bool) -> tuple[_Stub, Any, Engine]:
    stub = _Stub()

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter({"extract": stub})

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"consolidation": {"mine_facts": True, "mining_cache": cached}},
            },
            "semantic": {"enabled": True},
        },
    )
    await eng.start()
    return stub, eng._build_fact_miner(), eng


async def test_cached_mining_calls_the_model_once_per_transcript(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub, mine, eng = await _miner(monkeypatch, cached=True)
    try:
        first = await mine("[2023-05-08] Ana: I live in Lyon")
        second = await mine("[2023-05-08] Ana: I live in Lyon")
        await mine("[2023-05-09] Ana: something else")
    finally:
        await eng.stop()
    assert first == second and first[0].value == "Lyon"
    assert stub.calls == 2
    assert all(o.get("temperature") == 0.0 for o in stub.options)


async def test_uncached_mining_calls_every_time(monkeypatch: pytest.MonkeyPatch) -> None:
    stub, mine, eng = await _miner(monkeypatch, cached=False)
    try:
        await mine("[2023-05-08] Ana: I live in Lyon")
        await mine("[2023-05-08] Ana: I live in Lyon")
    finally:
        await eng.stop()
    assert stub.calls == 2
