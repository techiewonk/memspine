"""MOD-02: every built-in registers at import under today's config names."""

from __future__ import annotations

from types import SimpleNamespace

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
    assert _names("llm_providers") == ("llamacpp", "litellm")
    assert _names("vector_stores") == ("lance",)
    assert _names("lexical_indexes") == ("tantivy", "opensearch")
    assert _names("graph_stores") == ("sqlite_adjacency", "kuzu", "ladybug", "neo4j")
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
    register_reranker("mod02_tmp", lambda s: None)
    try:
        assert "mod02_tmp" in rerank_modes()
    finally:
        plugins.get_registry("rerankers").unregister("mod02_tmp")


def test_llm_provider_routing() -> None:
    from memspine.models.registry import llm_provider_name

    assert llm_provider_name("llamacpp/x.gguf") == "llamacpp"
    assert llm_provider_name("openai/gpt-4o") == "litellm"
    assert llm_provider_name("plain-model") == "litellm"


def test_unknown_embedder_keeps_message() -> None:
    from memspine.models.registry import build_embedder

    with pytest.raises(ConfigError, match=r"valid: fastembed, hash, static, litellm, st"):
        build_embedder(SimpleNamespace(provider="nope"))


def test_hash_embedder_builds_and_dim_checks() -> None:
    from memspine.models.registry import build_embedder

    assert build_embedder(SimpleNamespace(provider="hash")) is not None
    with pytest.raises(ConfigError, match=r"embedding.dim is required"):
        build_embedder(SimpleNamespace(provider="st", dim=None))


def test_unknown_provider_error_lists_valid() -> None:
    with pytest.raises(plugins.UnknownProviderError, match="valid: default"):
        plugins.get_registry("prompt_sets").get("nope")


def test_duplicate_registration_refused() -> None:
    reg = plugins.get_registry("prompt_sets")
    with pytest.raises(plugins.PluginError):
        reg.register("default", lambda: [])


def test_unknown_memory_type_still_rejected() -> None:
    from memspine.core.registry import validate_types

    validate_types({"episodic"})
    with pytest.raises(ConfigError, match="unknown memory type"):
        validate_types({"bogus"})
