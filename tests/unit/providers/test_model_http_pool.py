"""Model roles share transport leases while credentials stay per request."""

import httpx

from memspine.providers.contexts import LLMBuildContext, SharedBuildContext
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import get_registry, validate_settings
from memspine.providers.shared import ResourceRegistry
from memspine.services.llm.capabilities import schema_options


async def test_model_pool_lifetime_and_per_request_credentials(monkeypatch) -> None:
    monkeypatch.setenv("MODEL_A_KEY", "private-a")
    monkeypatch.setenv("MODEL_B_KEY", "private-b")
    registry = ResourceRegistry()
    owners = [ProviderLifecycleManager(close_timeout_s=2.0) for _ in range(2)]
    descriptor = get_registry("llm_providers").get("openai_compatible")
    services = []
    for index, owner in enumerate(owners):
        settings = validate_settings(
            descriptor,
            {
                "model": "model",
                "api_base": "http://localhost:8000/v1",
                "api_key_env": "MODEL_A_KEY" if index == 0 else "MODEL_B_KEY",
                "timeout_seconds": 2.0,
                "supports_json_schema": False,
                "supports_grammar": True,
            },
        )
        built = await descriptor.builder.build(
            settings, LLMBuildContext(SharedBuildContext(owner, "test", 1.0, registry), "chat")
        )
        services.append(built.service)
    first, second = services
    pool = first._connector._http
    assert second._connector._http is pool
    observed = []

    def respond(request):
        observed.append(request.headers["Authorization"])
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    pool._client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    assert await first.chat([{"role": "user", "content": "hello"}]) == "ok"
    assert (await owners[0].close_all()).complete
    assert await second.chat([{"role": "user", "content": "hello"}]) == "ok"
    assert observed == ["Bearer private-a", "Bearer private-b"]
    assert schema_options(second, {"type": "object"}, "answer")[0] == "grammar"
    client = pool._client
    assert (await owners[1].close_all()).complete
    assert client.is_closed
