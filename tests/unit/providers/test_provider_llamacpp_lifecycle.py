from __future__ import annotations

import asyncio
import sys
import threading
import types
from typing import Any

import pytest

from memspine.providers.builtin.models import register
from memspine.providers.contexts import LLMBuildContext, SharedBuildContext
from memspine.providers.lifecycle import LifecycleState, ProviderLifecycleManager
from memspine.providers.registry import LLM_PROVIDERS
from memspine.providers.settings import LlamaCppRoleSettings


def _builder():
    register()
    return LLM_PROVIDERS.get("llamacpp").builder


def _context(resources: ProviderLifecycleManager, role: str = "chat") -> LLMBuildContext:
    return LLMBuildContext(
        shared=SharedBuildContext(resources, "deployment-a", 1.0),
        role=role,
    )


def _settings() -> LlamaCppRoleSettings:
    return LlamaCppRoleSettings(model="llamacpp/test.gguf", timeout_seconds=2.0)


@pytest.mark.asyncio
async def test_llamacpp_builder_registers_before_construct_and_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = ProviderLifecycleManager(close_timeout_s=2.0)
    close_threads: list[int] = []
    constructed: dict[str, Any] = {}
    generation: dict[str, Any] = {}

    class FakeLlama:
        def __init__(self, **kwargs: Any) -> None:
            assert resources.states["llm:chat"] == LifecycleState.REGISTERED
            constructed.update(kwargs)

        def create_chat_completion(self, **kwargs: Any) -> dict[str, object]:
            generation.update(kwargs)
            return {"choices": [{"message": {"content": "answer"}}]}

        def close(self) -> None:
            close_threads.append(threading.get_ident())

    monkeypatch.setitem(sys.modules, "llama_cpp", types.SimpleNamespace(Llama=FakeLlama))
    result = await _builder().build(_settings(), _context(resources))
    assert len(result.resources) == 1
    assert await result.service.chat([{"role": "user", "content": "question"}]) == "answer"
    assert (constructed["n_ctx"], constructed["n_gpu_layers"], constructed["n_batch"]) == (
        8192,
        0,
        512,
    )
    assert (generation["temperature"], generation["top_p"], generation["max_tokens"]) == (
        0.8,
        0.95,
        2048,
    )
    await result.service.chat(
        [{"role": "user", "content": "question"}], temperature=0.1, max_tokens=32
    )
    assert (generation["temperature"], generation["top_p"], generation["max_tokens"]) == (
        0.1,
        0.95,
        32,
    )

    report = await resources.close_all()
    await result.service.close()
    assert report.complete
    assert len(close_threads) == 1
    assert close_threads[0] != threading.get_ident()


@pytest.mark.asyncio
async def test_llamacpp_constructor_failure_unwinds_registered_resource(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = ProviderLifecycleManager(close_timeout_s=2.0)
    registered_at_construct: list[bool] = []

    class FailingLlama:
        def __init__(self, **kwargs: Any) -> None:
            registered_at_construct.append("llm:chat" in resources.states)
            raise RuntimeError("native constructor failed")

    monkeypatch.setitem(sys.modules, "llama_cpp", types.SimpleNamespace(Llama=FailingLlama))
    with pytest.raises(RuntimeError, match="native constructor failed"):
        await _builder().build(_settings(), _context(resources))

    assert registered_at_construct == [True]
    report = await resources.close_all()
    assert report.complete
    assert resources.states == {"llm:chat": LifecycleState.STOPPED}


@pytest.mark.asyncio
async def test_close_waits_for_inflight_native_call_before_releasing_handle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resources = ProviderLifecycleManager(close_timeout_s=2.0)
    started = threading.Event()
    release = threading.Event()
    events: list[str] = []

    class BlockingLlama:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def create_chat_completion(self, **kwargs: Any) -> dict[str, object]:
            started.set()
            assert release.wait(2.0)
            events.append("chat-finished")
            return {"choices": [{"message": {"content": "answer"}}]}

        def close(self) -> None:
            events.append("closed")

    monkeypatch.setitem(sys.modules, "llama_cpp", types.SimpleNamespace(Llama=BlockingLlama))
    result = await _builder().build(_settings(), _context(resources))
    chat_task = asyncio.create_task(result.service.chat([{"role": "user", "content": "q"}]))
    assert await asyncio.to_thread(started.wait, 1.0)
    close_task = asyncio.create_task(result.service.close())
    await asyncio.sleep(0.01)
    assert events == []
    release.set()
    await chat_task
    await close_task
    assert events == ["chat-finished", "closed"]
    await resources.close_all()
