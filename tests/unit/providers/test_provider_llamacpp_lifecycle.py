from __future__ import annotations

import asyncio
import sys
import threading
import types
from typing import Any

import pytest

from memspine.config.schema import MemspineConfig
from memspine.providers.builtin.models import register
from memspine.providers.contexts import LLMBuildContext, SharedBuildContext
from memspine.providers.lifecycle import LifecycleState, ProviderLifecycleManager
from memspine.providers.registry import LLM_PROVIDERS
from memspine.providers.settings import LlamaCppRoleSettings
from memspine.runtime import container


def _builder():
    register()
    return LLM_PROVIDERS.get("llamacpp").builder


def _context(
    resources: ProviderLifecycleManager,
    role: str = "chat",
    resource_key: str | None = None,
) -> LLMBuildContext:
    return LLMBuildContext(
        shared=SharedBuildContext(resources, "deployment-a", 1.0),
        role=role,
        resource_key=resource_key,
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
async def test_named_llamacpp_profiles_have_distinct_resources_and_shared_profile_closes_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register()
    constructed: list[str] = []
    closed: dict[str, int] = {}

    class FakeLlama:
        def __init__(self, **kwargs: Any) -> None:
            model_path = str(kwargs["model_path"])
            self.model_path = model_path
            constructed.append(model_path)

        def create_chat_completion(self, **kwargs: Any) -> dict[str, object]:
            return {"choices": [{"message": {"content": "answer"}}]}

        def close(self) -> None:
            closed[self.model_path] = closed.get(self.model_path, 0) + 1

    monkeypatch.setitem(sys.modules, "llama_cpp", types.SimpleNamespace(Llama=FakeLlama))
    config = MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
        models={
            "profiles": {
                "primary": {
                    "kind": "llm",
                    "provider": "llamacpp",
                    "settings": {"model": "llamacpp/primary.gguf", "timeout_seconds": 2.0},
                },
                "backup": {
                    "kind": "llm",
                    "provider": "llamacpp",
                    "settings": {"model": "llamacpp/backup.gguf", "timeout_seconds": 2.0},
                },
            },
            "bindings": {"chat": "primary", "extract": "primary"},
            "escalation": {
                "chat": {
                    "targets": ["backup"],
                    "triggers": ["empty"],
                    "max_attempts": 2,
                    "timeout_s": 1.0,
                    "max_tokens_per_attempt": 64,
                }
            },
        },
    )
    resources = ProviderLifecycleManager(close_timeout_s=2.0)
    wiring = container.Wiring(
        resources=resources,
        health_timeout_s=2.0,
        startup_timeout_s=None,
    )

    router = await container.build_llm_router(config, container.WiringHooks(), wiring)
    assert constructed == ["primary.gguf", "backup.gguf"]
    assert resources.states == {
        "llm.profile:primary": LifecycleState.REGISTERED,
        "llm.profile:backup": LifecycleState.REGISTERED,
    }
    chat = router.provider("chat")
    assert chat._memspine_attempt_providers[0] is router.provider("extract")

    report = await resources.close_all()
    assert report.complete
    assert closed == {"primary.gguf": 1, "backup.gguf": 1}


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
