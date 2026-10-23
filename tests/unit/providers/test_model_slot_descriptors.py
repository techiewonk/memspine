"""RM-113 P2: model slots (llm, rerank, extraction, decider) are typed descriptors over
connectors; no construction outside the factories. Fakes only (no model, no network)."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec
from memspine.clients.weights.shared import PrivateWeightRegistry, SharedWeights
from memspine.providers.contexts import ExtractionBuildContext, SharedBuildContext
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import (
    DECIDERS,
    EXTRACTORS,
    LLM_PROVIDERS,
    RERANKERS,
    ProviderRegistryError,
    get_registry,
    validate_settings,
)
from memspine.services.extraction.base import ExtractionModels
from memspine.services.llm.tier_gate import is_local_binding


def _descriptor(kind: str, name: str) -> Any:
    get_registry(kind)
    return {
        "llm_providers": LLM_PROVIDERS,
        "rerankers": RERANKERS,
        "extractors": EXTRACTORS,
        "deciders": DECIDERS,
    }[kind].get(name)


# -- categories ------------------------------------------------------------------------------


def test_model_slot_categories() -> None:
    assert _descriptor("rerankers", "qwen3").ref.category == "rerank"
    assert _descriptor("extractors", "gliner2").ref.category == "extraction"
    assert _descriptor("deciders", "opendecider").ref.category == "decider"
    assert _descriptor("deciders", "gliner2").declared_capabilities == {"decider", "choose"}
    assert _descriptor("deciders", "heuristic").declared_capabilities == {"decider", "decide"}


# -- llm: ollama / vllm / bedrock are their own descriptors over the openai_compat connector --------


@pytest.mark.parametrize(
    ("name", "settings", "route", "api_base", "provider_id"),
    [
        (
            "ollama",
            {"model": "qwen3:8b", "timeout_seconds": 30.0},
            "qwen3:8b",
            "http://localhost:11434",
            "ollama:qwen3:8b",
        ),
        (
            "vllm",
            {"model": "Qwen/Qwen3-8B", "timeout_seconds": 30.0, "api_base": "http://gpu:8000/v1"},
            "Qwen/Qwen3-8B",
            "http://gpu:8000/v1",
            "vllm:Qwen/Qwen3-8B",
        ),
    ],
)
async def test_llm_route_is_fixed_by_the_descriptor(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    settings: dict[str, Any],
    route: str,
    api_base: str | None,
    provider_id: str,
) -> None:
    from scripts.model_transport_stub import install_transport, transport_stub as openai_compat

    install_transport(monkeypatch)

    seen: dict[str, Any] = {}

    class _Choice:
        message = type("M", (), {"content": "ok"})()

    class _Response:
        choices = [_Choice()]
        usage = None

    async def fake_acompletion(**kwargs: Any) -> _Response:
        seen.update(kwargs)
        return _Response()

    monkeypatch.setattr(openai_compat, "acompletion", fake_acompletion)
    descriptor = _descriptor("llm_providers", name)
    assert descriptor.requires_deployment_id
    built = await descriptor.builder.build(validate_settings(descriptor, settings), None)
    assert built.service.provider_id == provider_id
    assert await built.service.chat([{"role": "user", "content": "hi"}]) == "ok"
    assert seen["model"] == route
    assert built.service._connector.spec.api_base == api_base
    if name == "bedrock":
        assert seen["aws_region_name"] == "eu-west-1"


@pytest.mark.parametrize(
    ("name", "model"),
    [("ollama", "ollama/qwen3"), ("vllm", "hosted_vllm/x"), ("bedrock", "bedrock/x")],
)
def test_a_route_prefix_in_the_model_is_rejected(name: str, model: str) -> None:
    descriptor = _descriptor("llm_providers", name)
    raw: dict[str, Any] = {"model": model, "timeout_seconds": 5.0}
    if name == "vllm":
        raw["api_base"] = "http://gpu:8000/v1"
    with pytest.raises(ProviderRegistryError):
        validate_settings(descriptor, raw)


def test_locality_of_the_new_llm_providers() -> None:
    assert is_local_binding("ollama", {"model": "m"})
    assert not is_local_binding("ollama", {"model": "m", "api_base": "https://ollama.example"})
    assert is_local_binding("vllm", {"model": "m", "api_base": "http://127.0.0.1:8000/v1"})
    assert not is_local_binding("vllm", {"model": "m", "api_base": "https://vllm.example/v1"})
    assert not is_local_binding("bedrock", {"model": "m"})


# -- the openai_compat connector --------------------------------------------------------------------


async def test_openai_compat_connector_owns_transport_and_hides_the_key() -> None:
    connector = OpenAICompatConnector(
        OpenAICompatSpec(api_base="http://localhost:4000", api_key="sk-secret")
    )
    identity = connector.identity()
    assert "sk-secret" not in repr(identity) and identity["auth"]
    assert "sk-secret" not in repr(connector.spec)
    assert connector.locality() == "host"
    assert (
        OpenAICompatConnector(OpenAICompatSpec(api_base="https://api.example")).locality()
        == "cluster"
    )
    assert await connector.health() is True
    await connector.close()
    await connector.close()  # idempotent


# -- weights connector -------------------------------------------------------------------------


async def test_private_weight_registry_shares_within_itself_and_closes_last() -> None:
    registry = PrivateWeightRegistry()
    loads: list[int] = []
    closed: list[str] = []

    def connector() -> SharedWeights[str]:
        return SharedWeights(
            "model.test",
            {"model": "m"},
            None,
            lambda: (loads.append(1), "w")[1],
            close=closed.append,
            registry=registry,
        )

    a, b = connector(), connector()
    await a.open()
    assert a.handle == "w" and await a.health() and a.locality() == "process"
    assert await b.get() == "w" and loads == [1]
    await a.close()
    assert closed == []
    await b.close()
    assert closed == ["w"]
    assert not await a.health()


# -- extraction descriptor ---------------------------------------------------------------------


async def test_extraction_descriptor_builds_the_engine_pool_and_owns_its_close() -> None:
    descriptor = _descriptor("extractors", "gliner2")
    resources = ProviderLifecycleManager(close_timeout_s=2.0)
    ctx = ExtractionBuildContext(SharedBuildContext(resources, "deployment-a", 1.0))
    built = await descriptor.builder.build(validate_settings(descriptor, {"max_models": 2}), ctx)
    assert isinstance(built.service, ExtractionModels)
    assert [ref.resource_id for ref in built.resources] == ["gliner-model-pool"]
    await resources.close_all()
    with pytest.raises(ProviderRegistryError):
        validate_settings(descriptor, {"max_models": 0})


# -- decider descriptors -----------------------------------------------------------------------


def test_decider_selection_goes_through_the_descriptors() -> None:
    from memspine.providers.decider import build_decider, build_decision_provider

    assert build_decider("heuristic").decider_id == "heuristic"
    opendecider = build_decider("opendecider", "m", "cpu", threads=2, workers=2)
    assert opendecider.decider_id == "opendecider" and opendecider.threads == 2
    with pytest.raises(ValueError):
        build_decider("jepa")
    from memspine.config.schema.model import GlinerSettings
    from memspine.exceptions import ConfigError

    with pytest.raises(ConfigError, match="GLiNER pool"):
        build_decision_provider(GlinerSettings(tasks=("classification",)), None)
