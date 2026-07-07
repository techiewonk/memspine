"""LLM port: router, OpenAI-compatible provider (MockTransport), json repair."""

from __future__ import annotations

import httpx
import pytest

from memspine.clients.http import HTTPClient
from memspine.exceptions import ConfigError, LLMError, MissingServiceError
from memspine.services.llm.base import LLMRouter, lenient_json
from memspine.services.llm.local import OpenAICompatLLM


def mock_http(handler: httpx.MockTransport) -> HTTPClient:
    return HTTPClient(transport=handler)


async def test_openai_compat_happy_path_and_auth_header() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers.get("Authorization", "")
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "hello from ollama"}}]}
        )

    http = mock_http(httpx.MockTransport(handler))
    await http.connect()
    llm = OpenAICompatLLM(http, "http://localhost:11434/v1", "llama3", api_key="sk-x")
    reply = await llm.chat([{"role": "user", "content": "hi"}])
    assert reply == "hello from ollama"
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["auth"] == "Bearer sk-x"


async def test_http_error_and_bad_shape_raise_llm_error() -> None:
    http = mock_http(httpx.MockTransport(lambda _req: httpx.Response(500, text="boom")))
    await http.connect()
    llm = OpenAICompatLLM(http, "http://x/v1", "m")
    with pytest.raises(LLMError, match="500"):
        await llm.chat([{"role": "user", "content": "hi"}])

    http2 = mock_http(httpx.MockTransport(lambda _req: httpx.Response(200, json={"odd": 1})))
    await http2.connect()
    llm2 = OpenAICompatLLM(http2, "http://x/v1", "m")
    with pytest.raises(LLMError, match="unexpected response shape"):
        await llm2.chat([{"role": "user", "content": "hi"}])


def test_router_names_missing_role() -> None:
    router = LLMRouter({})
    with pytest.raises(ConfigError, match=r"llm\.roles\.judge"):
        router.for_role("judge")


def test_lenient_json_repairs_llm_output() -> None:
    # trailing comma + unquoted key + missing brace: classic LLM near-JSON
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
