from __future__ import annotations

import json

import pytest
from pydantic import Field

from memspine.providers import provider_settings_reference
from memspine.providers.contracts import ProviderRef
from memspine.providers.registry import ProviderDescriptor, get_registry, validate_settings
from memspine.providers.settings import StrictSettings


@pytest.mark.parametrize(
    ("category", "provider", "key", "expected"),
    [
        ("lexical", "tantivy", "analyzer", "default"),
        ("vector", "lancedb", "quantization", "auto"),
        ("vector", "pgvector", "hnsw_m", 16),
        ("vector", "weaviate", "collection_prefix", "Memspine"),
        ("lexical", "weaviate", "collection_prefix", "Memspine"),
        ("lexical", "opensearch", "index_prefix", "memspine"),
        ("cache", "disk", "namespace", "memspine"),
        ("graph", "neo4j", "database", "neo4j"),
        ("rerank", "qwen3", "batch_size", 8),
        ("rerank", "jina", "max_documents", 64),
    ],
)
def test_each_provider_publishes_its_native_defaults(category, provider, key, expected) -> None:
    reference = provider_settings_reference(category, provider)
    assert reference["defaults"][key] == expected
    assert reference["schema"]["additionalProperties"] is False


def test_required_deadlines_are_not_invented_and_defaults_are_detached() -> None:
    first = provider_settings_reference("storage", "sqlite")
    assert "busy_timeout_s" in first["required"]
    assert "busy_timeout_s" not in first["defaults"]
    first["defaults"]["path"] = "changed"
    assert provider_settings_reference("storage", "sqlite")["defaults"]["path"] == "./memspine.db"


def test_discovery_does_not_run_factories_or_provider_builders_and_redacts_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    class Settings(StrictSettings):
        api_key: str = "private-static-default"
        cache: dict[str, str] = Field(default_factory=lambda: called.append("factory") or {})

    class Builder:
        async def build(self, settings, context):
            called.append("builder")
            raise AssertionError("discovery must not build")

    registry = get_registry("llm_providers")
    descriptor = ProviderDescriptor(
        ProviderRef("llm", "defaults-test"), Settings, frozenset({"llm"}), (), Builder()
    )
    monkeypatch.setitem(registry._items, descriptor.ref.name, descriptor)
    reference = provider_settings_reference("llm", descriptor.ref.name)
    assert called == []
    assert "private-static-default" not in json.dumps(reference)
    assert reference["defaults"]["api_key"] == "<redacted>"
    assert reference["defaults_complete"] is False


def test_discovery_does_not_run_nested_default_factories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []

    class NestedSettings(StrictSettings):
        values: dict[str, str] = Field(
            default_factory=lambda: called.append("nested factory") or {}
        )

    class Settings(StrictSettings):
        nested: NestedSettings | None = None

    class Builder:
        async def build(self, settings, context):
            called.append("builder")
            raise AssertionError("discovery must not build")

    registry = get_registry("llm_providers")
    descriptor = ProviderDescriptor(
        ProviderRef("llm", "nested-defaults-test"), Settings, frozenset({"llm"}), (), Builder()
    )
    monkeypatch.setitem(registry._items, descriptor.ref.name, descriptor)

    reference = provider_settings_reference("llm", descriptor.ref.name)

    assert called == []
    assert reference["defaults_complete"] is False


def test_discovery_redacts_credentials_embedded_in_endpoint_defaults(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class Settings(StrictSettings):
        endpoint: str = "https://user:private-pass@example.test/api?api_key=private-key&region=west"

    class Builder:
        async def build(self, settings, context):
            raise AssertionError("discovery must not build")

    registry = get_registry("llm_providers")
    descriptor = ProviderDescriptor(
        ProviderRef("llm", "endpoint-defaults-test"),
        Settings,
        frozenset({"llm"}),
        (),
        Builder(),
    )
    monkeypatch.setitem(registry._items, descriptor.ref.name, descriptor)

    reference = provider_settings_reference("llm", descriptor.ref.name)
    assert "private-pass" not in json.dumps(reference)
    assert "private-key" not in json.dumps(reference)
    assert reference["defaults"]["endpoint"] == (
        "https://<redacted>@example.test/api?api_key=%3Credacted%3E&region=west"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("overrides", [{}, {"batch_size": 3, "max_length": 512}])
async def test_qwen_reranker_native_defaults_and_overrides_reach_constructor(
    monkeypatch: pytest.MonkeyPatch, overrides: dict[str, int]
) -> None:
    from memspine.services.rerank import qwen3 as qwen3_rerank

    seen = {}

    class FakeReranker:
        def __init__(self, model, **kwargs):
            seen.update(model=model, **kwargs)

    monkeypatch.setattr(qwen3_rerank, "Qwen3Reranker", FakeReranker)
    descriptor = get_registry("rerankers").get("qwen3")
    settings = validate_settings(descriptor, overrides)
    await descriptor.builder.build(settings, None)
    assert seen["model"] == "Qwen/Qwen3-Reranker-0.6B"
    assert seen["batch_size"] == overrides.get("batch_size", 8)
    assert seen["max_length"] == overrides.get("max_length", 8192)
