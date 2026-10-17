"""The centralized provider registry owns all built-in identities."""

from __future__ import annotations

import pytest

from memspine import plugins
from memspine.exceptions import ConfigError


def _names(kind: str) -> tuple[str, ...]:
    # Built-ins only: other tests may leave test providers in a registry.
    return plugins.get_registry(kind).builtin_names()


def test_all_kinds_exist() -> None:
    for kind in plugins.KINDS:
        assert plugins.get_registry(kind).kind == kind


def test_builtin_names_match_config_values() -> None:
    assert _names("embedders") == ("fastembed", "hash", "static", "litellm", "st")
    assert _names("rerankers") == ("fastembed", "flashrank", "litellm", "qwen3", "jina")
    assert _names("llm_providers") == ("litellm", "llamacpp")
    assert set(_names("vector_stores")) == {"lancedb", "pgvector", "weaviate"}
    assert set(_names("lexical_indexes")) == {"tantivy", "opensearch", "weaviate"}
    assert _names("graph_stores") == ("sqlite_adjacency", "ladybug", "neo4j")
    assert _names("prompt_sets") == ("default",)
    assert set(_names("derive_stages")) == {
        "compile",
        "ground",
        "integrate",
        "coverage",
        "histories",
    }


def test_memory_types_match_core_table() -> None:
    from memspine.core.registry import MEMORY_TYPES

    assert set(_names("memory_types")) == set(MEMORY_TYPES)


def test_read_legs_families() -> None:
    from memspine.read.registry import leg_family

    assert leg_family("vector") == "semantic"
    assert leg_family("lexical") == "lexical"
    assert leg_family("speaker_vote:alice") == "semantic"
    assert leg_family("temporal_window") == "temporal"
    assert leg_family("mystery") == "other"


def test_rerank_modes_come_from_the_registry() -> None:
    from memspine.models.providers.rerank.factory import register_reranker, rerank_modes

    modes = rerank_modes()
    assert modes[:6] == ("off", "fastembed", "flashrank", "litellm", "qwen3", "jina")
    from memspine.providers.contracts import BuildResult, ProviderRef
    from memspine.providers.registry import ProviderDescriptor
    from memspine.providers.settings import EmptySettings

    class Builder:
        async def build(self, settings, ctx):
            return BuildResult(object(), ProviderRef("reranker", "mod02_tmp"), (), frozenset({"rerank"}))

    register_reranker(ProviderDescriptor(
        ProviderRef("reranker", "mod02_tmp"), EmptySettings,
        frozenset({"rerank"}), (), Builder(),
    ))
    try:
        assert "mod02_tmp" in rerank_modes()
    finally:
        plugins.get_registry("rerankers").unregister("mod02_tmp")


def test_llm_provider_is_declared_separately_from_model_id() -> None:
    from memspine.config.schema.llm import ModelDecl, ModelsConfig
    from memspine.models.resolve import resolve_models

    models = ModelsConfig(
        tiers_v2=True,
        catalogue=[
            ModelDecl(
                id="ollama/model",
                provider="llamacpp",
                timeout_seconds=30.0,
            )
        ],
        role_tier={"extract": "worker"},
    )
    resolved = resolve_models(models, gpus=())
    assert resolved.plan.binding("extract").provider == "llamacpp"


def test_embedder_factories_are_typed_and_lazy() -> None:
    from memspine.providers.registry import EMBEDDERS, ProviderDescriptor

    fastembed = EMBEDDERS.get("fastembed")
    assert isinstance(fastembed, ProviderDescriptor)
    assert fastembed.settings_model.__name__ == "FastembedEmbeddingSettings"
    assert "fastembed" in EMBEDDERS.builtin_names()


def test_unknown_provider_error_lists_valid() -> None:
    with pytest.raises(plugins.UnknownProviderError, match="valid: default"):
        plugins.get_registry("prompt_sets").get("nope")


def test_obsolete_vector_provider_name_is_not_an_alias() -> None:
    with pytest.raises(plugins.UnknownProviderError, match="unknown vector_stores provider 'lance'") as caught:
        plugins.get_registry("vector_stores").get("lance")
    assert "lancedb" in str(caught.value)


def test_duplicate_registration_refused() -> None:
    reg = plugins.get_registry("prompt_sets")
    with pytest.raises(plugins.ProviderRegistryError):
        reg.register("default", lambda: [], replace=False)


def test_unknown_memory_type_still_rejected() -> None:
    from memspine.core.registry import validate_types

    validate_types({"episodic"})
    with pytest.raises(ConfigError, match="unknown memory type"):
        validate_types({"bogus"})
