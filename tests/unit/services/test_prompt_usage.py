"""#33: per-prompt token tracking (``LLMRouter.prompt_usage`` / ``Engine.usage``)."""

from __future__ import annotations

import pickle
from typing import Any

import orjson
import pytest
from structlog.testing import capture_logs

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.observability.logging import configure_logging
from memspine.observability.usage import UNNAMED_PROMPT
from memspine.prompts.base import RenderedMessages
from memspine.prompts.registry import PromptRegistry
from memspine.services.llm.base import LLMRouter, LLMService


class _Stub:
    def __init__(self, reply: str, usage: tuple[int, int] | None = None) -> None:
        self.reply = reply
        self.calls = 0
        self._usage = usage
        self.last_usage: tuple[int, int] | None = None

    @property
    def provider_id(self) -> str:
        return "stub"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls += 1
        self.last_usage = self._usage
        return self.reply


def test_rendered_messages_carry_prompt_identity_and_stay_a_list() -> None:
    prompt = PromptRegistry().get("summarize")
    messages = prompt.render({"content": "Ana moved to Lyon."})
    assert isinstance(messages, RenderedMessages)
    assert messages.prompt_id == "summarize"
    assert messages.prompt_version == prompt.prompt_version
    assert messages == list(messages)  # compares as a plain list
    assert orjson.loads(orjson.dumps(messages)) == list(messages)
    restored = pickle.loads(pickle.dumps(messages))
    assert restored == messages and restored.prompt_version == prompt.prompt_version


async def test_router_attributes_reported_tokens_to_the_prompt() -> None:
    router = LLMRouter({"summarize": _Stub("short", usage=(120, 7))})
    prompt = PromptRegistry().get("summarize")
    await router.for_role("summarize").chat(prompt.render({"content": "x" * 40}))
    await router.for_role("summarize").chat(prompt.render({"content": "y" * 40}))
    usage = router.prompt_usage()
    entry = usage[prompt.prompt_version]
    assert entry["prompt_id"] == "summarize"
    assert entry["roles"] == ["summarize"]
    assert entry["calls"] == 2
    assert entry["input_tokens"] == 240 and entry["output_tokens"] == 14
    assert entry["estimated_calls"] == 0 and entry["estimated"] is False


async def test_router_estimates_and_flags_when_provider_reports_nothing() -> None:
    router = LLMRouter({"chat": _Stub("a" * 40)})
    await router.for_role("chat").chat([{"role": "user", "content": "b" * 80}])
    entry = router.prompt_usage()[UNNAMED_PROMPT]
    assert entry["prompt_version"] is None
    assert entry["input_tokens"] == 20 and entry["output_tokens"] == 10
    assert entry["estimated"] is True and entry["estimated_calls"] == 1


async def test_reset_clears_after_the_snapshot() -> None:
    router = LLMRouter({"chat": _Stub("ok")})
    await router.for_role("chat").chat([{"role": "user", "content": "hi"}])
    first = router.prompt_usage(reset=True)
    assert first[UNNAMED_PROMPT]["calls"] == 1
    assert router.prompt_usage() == {}
    assert router.call_counts() == {"chat": 1}  # role counts are not reset


async def test_usage_event_is_logged_at_debug() -> None:
    configure_logging(level="DEBUG")
    try:
        router = LLMRouter({"chat": _Stub("ok", usage=(3, 1))})
        with capture_logs() as logs:
            await router.for_role("chat").chat([{"role": "user", "content": "hi"}])
        events = [e for e in logs if e["event"] == "llm.usage"]
        assert events and events[0]["input_tokens"] == 3 and events[0]["estimated"] is False
    finally:
        configure_logging()


async def test_engine_usage_per_prompt_with_stub_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    stubs: dict[str, LLMService] = {"extract": _Stub('{"facts": []}', usage=(50, 5))}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True, "policies": {"entity_extraction": "llm"}}},
        read={"hybrid": False},
    )
    await eng.start()
    try:
        assert eng.usage() == {}
        await eng.write("Ana lives in Lyon", namespace="a", memory_type="semantic")
        usage = eng.usage()
        assert len(usage) == 1
        ((key, entry),) = usage.items()
        assert key.startswith("extract@") and entry["prompt_id"] == "extract"
        assert entry["calls"] == 1 and entry["input_tokens"] == 50
        assert eng.usage(reset=True) == usage
        assert eng.usage() == {}
    finally:
        await eng.stop()
