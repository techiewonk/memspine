from __future__ import annotations

import types
from typing import Any

import pytest

from memspine.providers.builtin.models import register
from memspine.providers.contexts import LLMBuildContext, SharedBuildContext
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import LLM_PROVIDERS
from memspine.providers.settings import OpenAICompatRoleSettings
from memspine.services.llm.base import LLMRouter


@pytest.mark.asyncio
async def test_openai_compat_settings_flow_to_native_defaults_and_call_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register()
    seen: dict[str, Any] = {}

    class Response:
        choices = [types.SimpleNamespace(message=types.SimpleNamespace(content="ok"))]
        usage = None

    async def fake_acompletion(**kwargs: Any) -> Response:
        seen.update(kwargs)
        return Response()

    from scripts.model_transport_stub import install_transport
    from scripts.model_transport_stub import transport_stub as openai_compat

    install_transport(monkeypatch)

    monkeypatch.setattr(openai_compat, "acompletion", fake_acompletion)
    descriptor = LLM_PROVIDERS.get("openai_compatible")
    resources = ProviderLifecycleManager(close_timeout_s=1.0)
    context = LLMBuildContext(SharedBuildContext(resources, "deployment-test", 1.0), "chat")
    settings = OpenAICompatRoleSettings(
        model="openai/test",
        timeout_seconds=5.0,
        temperature=0.6,
        top_p=0.7,
        max_tokens=900,
    )

    built = await descriptor.builder.build(settings, context)
    router = LLMRouter({"chat": built.service})
    reply = await router.for_role("chat").chat([{"role": "user", "content": "hello"}])

    assert reply == "ok"
    assert (seen["temperature"], seen["top_p"], seen["max_tokens"]) == (0.6, 0.7, 900)
    assert built.service._timeout == 5.0

    reply = await router.for_role("chat").chat(
        [{"role": "user", "content": "hello"}], temperature=0.0, max_tokens=64
    )
    assert reply == "ok"
    assert (seen["temperature"], seen["top_p"], seen["max_tokens"]) == (0.0, 0.7, 64)
