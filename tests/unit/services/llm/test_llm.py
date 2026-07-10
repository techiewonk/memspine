"""LLM port: router, LiteLLM provider (mocked), json repair, llama.cpp guard."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.exceptions import ConfigError, LLMError, MissingServiceError
from memspine.services.llm.base import LLMRouter, lenient_json
from memspine.services.llm.litellm_llm import LiteLLMLLM


class _Msg:
    def __init__(self, content: str | None) -> None:
        self.content = content


class _Choice:
    def __init__(self, content: str | None) -> None:
        self.message = _Msg(content)


class _Response:
    def __init__(self, content: str | None) -> None:
        self.choices = [_Choice(content)]


async def test_litellm_chat_passes_model_and_options(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    seen: dict[str, Any] = {}

    async def fake_acompletion(**kwargs: Any) -> _Response:
        seen.update(kwargs)
        return _Response("hello from the cloud")

    monkeypatch.setattr(litellm, "acompletion", fake_acompletion)
    llm = LiteLLMLLM("bedrock/anthropic.claude-3", aws_region="us-east-1", api_base="http://x")
    reply = await llm.chat([{"role": "user", "content": "hi"}], temperature=0.2)
    assert reply == "hello from the cloud"
    assert seen["model"] == "bedrock/anthropic.claude-3"
    assert seen["aws_region_name"] == "us-east-1"
    assert seen["api_base"] == "http://x"
    assert seen["temperature"] == 0.2  # extra options forwarded
    assert llm.provider_id == "litellm:bedrock/anthropic.claude-3"


async def test_litellm_wraps_provider_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    async def boom(**_kwargs: Any) -> _Response:
        raise RuntimeError("429 rate limited")

    monkeypatch.setattr(litellm, "acompletion", boom)
    llm = LiteLLMLLM("openai/gpt-4o")
    with pytest.raises(LLMError, match="litellm chat failed"):
        await llm.chat([{"role": "user", "content": "hi"}])


async def test_litellm_empty_content_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    async def none_content(**_kwargs: Any) -> _Response:
        return _Response(None)

    monkeypatch.setattr(litellm, "acompletion", none_content)
    llm = LiteLLMLLM("openai/gpt-4o")
    with pytest.raises(LLMError, match="empty content"):
        await llm.chat([{"role": "user", "content": "hi"}])


def test_router_names_missing_role() -> None:
    router = LLMRouter({})
    with pytest.raises(ConfigError, match=r"llm\.roles\.judge"):
        router.for_role("judge")


def test_lenient_json_repairs_llm_output() -> None:
    # trailing comma + missing brace: classic LLM near-JSON
    assert lenient_json('{"facts": ["a", "b",], "count": 2') == {
        "facts": ["a", "b"],
        "count": 2,
    }


def test_llama_cpp_without_extra_names_the_fix() -> None:
    import builtins

    real_import = builtins.__import__

    def no_llama(name: str, *args: object, **kwargs: object) -> object:
        if name == "llama_cpp":
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)  # type: ignore[arg-type]

    builtins.__import__ = no_llama  # type: ignore[assignment]
    try:
        from memspine.services.llm.llama_cpp import LlamaCppLLM

        with pytest.raises(MissingServiceError, match=r"memspine\[llmlocal\]"):
            LlamaCppLLM(model_path="model.gguf")
    finally:
        builtins.__import__ = real_import
