from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import SecretStr

from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.registry import (
    EMBEDDERS,
    KINDS,
    ProviderDescriptor,
    ProviderRegistryError,
    Registry,
    get_registry,
    provider_target_identity,
    validate_settings,
)
from memspine.providers.settings import EmptySettings, StrictSettings


class _SecretSettings(StrictSettings):
    token: str


class _Builder:
    async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[str]:
        return BuildResult("ok", ProviderRef("embedding", "registry_test"), (), frozenset())


def test_central_registry_is_unique_per_kind_and_includes_provider_kinds() -> None:
    assert Registry("embedders") is get_registry("embedders") is EMBEDDERS
    assert {"storage_backends", "cache_backends"} <= KINDS.keys()


def test_legacy_flat_provider_options_are_rejected() -> None:
    from pydantic import ValidationError

    from memspine.config.schema.storage import CacheConfig, StorageConfig, VectorConfig

    for model, payload in (
        (StorageConfig, {"path": ":memory:"}),
        (CacheConfig, {"backend": "disk", "path": "cache"}),
        (VectorConfig, {"isolation": "per_namespace"}),
    ):
        with pytest.raises(ValidationError):
            model.model_validate(payload)


def test_provider_registries_contain_descriptors_instead_of_none_sentinels() -> None:
    from memspine.providers.registry import get_registry as registry_for

    registry = registry_for("embedders")
    assert registry.names()
    assert all(isinstance(value, ProviderDescriptor) for _, value in registry.items())


def test_builtin_optional_model_packages_are_discoverable_without_importing_them(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from memspine.exceptions import MissingServiceError
    from memspine.providers.registry import LLM_PROVIDERS, RERANKERS, get_registry
    from memspine.providers.selection import select_provider

    get_registry("embedders")
    get_registry("llm_providers")
    get_registry("rerankers")
    static = EMBEDDERS.get("static")
    local_llm = LLM_PROVIDERS.get("llamacpp")
    flashrank = RERANKERS.get("flashrank")
    assert static.requirements[0].name == "model2vec"
    assert static.service_manifest is not None and static.service_manifest.extra == "static"
    assert local_llm.requirements[0].name == "llama_cpp"
    assert local_llm.service_manifest is not None and local_llm.service_manifest.extra == "llmlocal"
    assert flashrank.requirements[0].name == "flashrank"
    assert flashrank.service_manifest is not None and flashrank.service_manifest.extra == "rerank"

    monkeypatch.setattr("importlib.util.find_spec", lambda _name: None)
    with pytest.raises(MissingServiceError, match=r"memspine\[static\]"):
        select_provider("embedding", "static")
    with pytest.raises(MissingServiceError, match=r"memspine\[llmlocal\]"):
        select_provider("llm", "llamacpp")
    with pytest.raises(MissingServiceError, match=r"memspine\[rerank\]"):
        select_provider("reranker", "flashrank")


def test_duplicate_provider_name_is_rejected() -> None:
    registry = get_registry("embedders")
    descriptor = ProviderDescriptor(
        ProviderRef("embedding", "registry_test"), EmptySettings, frozenset(), (), _Builder()
    )
    registry.register("registry_test", descriptor)
    try:
        with pytest.raises(ProviderRegistryError, match="already registered"):
            registry.register("registry_test", descriptor)
    finally:
        registry.unregister("registry_test")


def test_registry_rejects_unsupported_descriptor_contract_version() -> None:
    registry = get_registry("embedders")
    descriptor = ProviderDescriptor(
        ProviderRef("embedding", "unsupported_contract", contract_version=2),
        EmptySettings,
        frozenset(),
        (),
        _Builder(),
    )
    with pytest.raises(ProviderRegistryError, match="contract version"):
        registry.register("unsupported_contract", descriptor, builtin=False)


def test_settings_validation_does_not_echo_secret_values() -> None:
    descriptor = ProviderDescriptor(
        ProviderRef("embedding", "secret_test"), _SecretSettings, frozenset(), (), _Builder()
    )
    with pytest.raises(ProviderRegistryError) as exc:
        validate_settings(descriptor, {"unexpected": "super-secret-value"})
    assert "unexpected" in str(exc.value)
    assert "super-secret-value" not in str(exc.value)


def test_provider_target_identity_is_json_safe_and_immutable() -> None:
    descriptor = ProviderDescriptor(
        ProviderRef("embedding", "target_identity_test"),
        EmptySettings,
        frozenset(),
        (),
        _Builder(),
        target_identity=lambda _settings: {"endpoint": "https://example.invalid", "port": 42},
    )
    identity = provider_target_identity(descriptor, EmptySettings())
    assert identity is not None
    assert identity == {"endpoint": "https://example.invalid", "port": 42}
    with pytest.raises(TypeError):
        identity["endpoint"] = "changed"  # type: ignore[index]


async def test_same_service_can_have_two_categories_and_share_one_resource() -> None:
    """A backend can expose vector and lexical services with independent settings."""

    class VectorSettings(StrictSettings):
        endpoint: str
        token: SecretStr

    class LexicalSettings(StrictSettings):
        index: str

    closed: list[str] = []

    async def close() -> None:
        closed.append("shared")

    class VectorBuilder:
        async def build(self, settings: VectorSettings, ctx: Any) -> BuildResult[str]:
            resource = ctx.resources.own("shared", close)
            return BuildResult("vector", ProviderRef("vector", "test_dual_backend"), (resource,), frozenset({"vector"}))

    class LexicalBuilder:
        async def build(self, settings: LexicalSettings, ctx: Any) -> BuildResult[str]:
            resource = ctx.resources.borrow("shared")
            return BuildResult("lexical", ProviderRef("lexical", "test_dual_backend"), (resource,), frozenset({"lexical"}))

    vector = ProviderDescriptor(
        ProviderRef("vector", "test_dual_backend"), VectorSettings, frozenset({"vector"}), (), VectorBuilder()
    )
    lexical = ProviderDescriptor(
        ProviderRef("lexical", "test_dual_backend"), LexicalSettings, frozenset({"lexical"}), (), LexicalBuilder()
    )
    vector_registry = get_registry("vector_stores")
    lexical_registry = get_registry("lexical_indexes")
    vector_registry.register(vector.ref.name, vector, builtin=False)
    lexical_registry.register(lexical.ref.name, lexical, builtin=False)
    lifecycle = ProviderLifecycleManager(close_timeout_s=1)
    ctx = SimpleNamespace(resources=lifecycle)
    try:
        assert vector_registry.get(vector.ref.name).settings_model is VectorSettings
        assert lexical_registry.get(lexical.ref.name).settings_model is LexicalSettings
        from memspine.providers.catalog import catalog_dicts

        rows = catalog_dicts(category="vector")
        dual_row = next(row for row in rows if row["name"] == "test_dual_backend")
        assert "endpoint" not in str(dual_row)
        assert "token" not in str(dual_row)
        assert "secret" not in str(dual_row)
        built_vector = await vector.builder.build(
            VectorSettings(endpoint="https://host.example/path?token=secret", token="secret"), ctx
        )
        built_lexical = await lexical.builder.build(LexicalSettings(index="memories"), ctx)
        assert built_vector.resources[0].ownership == "owned"
        assert built_lexical.resources[0].ownership == "borrowed"
        report = await lifecycle.close_all()
        assert report.closed == ("shared",)
        assert closed == ["shared"]
    finally:
        vector_registry.unregister(vector.ref.name)
        lexical_registry.unregister(lexical.ref.name)
