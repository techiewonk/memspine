"""FX-EVAL: the adapter fires session close (call 2) per ingested session before the build sleep,
and awaits the ``SleepHandle`` that ``offline.sleep`` returns. Stub LLM, hash embedder, no GPU."""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

pytest.importorskip("memspine")
from memspine_evals.contracts import Turn
from memspine_evals.systems.memspine_system import MemspineSystem

LINE = re.compile(r"^\[(\d+)\] \S+ (\S+): (.+)$", re.M)
_TEXTS = (
    "I repotted the orchid on the balcony",
    "We booked a kayak tour along the northern coast",
    "The violin recital starts at seven on Friday",
)


class _CloseLLM:
    def __init__(self) -> None:
        self.prompts = 0

    @property
    def provider_id(self) -> str:
        return "fake:close"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.prompts += 1
        lines = [int(n) for n, _, _ in LINE.findall(messages[-1]["content"])]
        return json.dumps(
            {
                "summary": {"text": "Ilse talks about plants and trips."},
                "episodes": [
                    {"text": "Ilse discussed her orchid and a kayak tour.", "lines": lines}
                ],
                "profile_ops": [],
            }
        )


def _config(**offline: Any) -> dict[str, Any]:
    return {
        "model": {
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "session_close": {
                    "provider": "openai_compatible",
                    "timeout_s": 30.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "fake/close"},
                }
            },
        },
        "deployment": {"id": "unit-test"},
        "dotenv_path": None,
        "memories": {"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        "offline": {"session_close": True, **offline},
    }


async def _run(
    monkeypatch: pytest.MonkeyPatch, config: dict[str, Any]
) -> tuple[Any, Any, _CloseLLM]:
    llm = _CloseLLM()

    async def router(cfg: Any, hooks: object = None, wiring: object = None) -> Any:
        from memspine.services.llm.base import LLMRouter

        return LLMRouter({"session_close": llm})  # type: ignore[dict-item]

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    system = MemspineSystem(config=config, build_sleep=True)
    await system.reset("item")
    for sid, day in (("s1", "01"), ("s2", "02")):
        for i, text in enumerate(_TEXTS):
            await system.insert(
                Turn(f"{sid}-{i}", sid, "Ilse", text, timestamp=f"2023-05-{day}T10:0{i}:00")
            )
    built = await system.build()
    return system, built, llm


def _episodes(records: list[Any]) -> list[Any]:
    return [r for r in records if r.source.channel == "session_close" and "kind:episode" in r.tags]


def test_build_closes_each_session_and_writes_l2_episodes(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        system, built, llm = await _run(monkeypatch, _config())
        try:
            assert llm.prompts == 2  # one call-2 per ingested session
            assert sorted(built.meta["session_close"]) == ["s1", "s2"]
            assert all(v["hook"]["status"] == "ok" for v in built.meta["session_close"].values())
            records = await system._engine._require_started().list_records("eval", "semantic")
            assert len(_episodes(records)) == 2
        finally:
            await system.close()

    asyncio.run(go())


def test_session_close_off_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        cfg = _config()
        cfg["offline"] = {}
        system, built, llm = await _run(monkeypatch, cfg)
        try:
            assert llm.prompts == 0 and "session_close" not in built.meta
        finally:
            await system.close()

    asyncio.run(go())


def test_sleep_v2_handle_is_awaited(monkeypatch: pytest.MonkeyPatch) -> None:
    async def go() -> None:
        system, built, _ = await _run(monkeypatch, _config(sleep=True))
        try:
            sleep = built.meta["sleep"]
            assert sleep and all(isinstance(v, dict) for v in sleep.values())
            assert "consolidate" in sleep
        finally:
            await system.close()

    asyncio.run(go())


def test_await_sleep_passes_dicts_and_waits_handles() -> None:
    class _Handle:
        async def wait(self) -> dict[str, dict[str, object]]:
            return {"consolidate": {"status": "ok"}}

    assert asyncio.run(MemspineSystem._await_sleep(_Handle())) == {"consolidate": {"status": "ok"}}
    assert asyncio.run(MemspineSystem._await_sleep({"a": {}})) == {"a": {}}
