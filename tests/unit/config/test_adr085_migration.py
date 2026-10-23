import pytest
from scripts.migrate_config_keys_adr085 import migrate


@pytest.mark.parametrize(
    ("route", "provider", "bare"),
    [
        ("ollama/llama3", "ollama", "llama3"),
        ("ollama_chat/qwen3", "ollama", "qwen3"),
        ("hosted_vllm/Qwen/Qwen3", "vllm", "Qwen/Qwen3"),
        ("bedrock/anthropic.x", "bedrock", "anthropic.x"),
        ("bedrock/converse/qwen.qwen3", "bedrock", "qwen.qwen3"),
        ("anthropic/claude", "anyllm", "claude"),
        ("openai/gpt-4", "openai_compatible", "gpt-4"),
    ],
)
def test_migration_idempotent(route, provider, bare):
    original = {
        "model": {
            "llm": {
                "chat": {
                    "provider": "litellm",
                    "timeout_s": 10.0,
                    "settings": {"model": route, "api_base": "http://localhost:8000/v1"},
                }
            }
        }
    }
    if route.startswith("bedrock/"):
        original["model"]["llm"]["chat"]["settings"].pop("api_base")
    result = migrate(original)
    slot = result["model"]["llm"]["chat"]
    assert slot["provider"] == provider
    assert slot["settings"]["model"] == bare
    assert result == migrate(result)
    assert original["model"]["llm"]["chat"]["provider"] == "litellm"


def test_removed_name_rejected():
    from memspine.config.schema.model import ModelSlot
    from memspine.exceptions import ConfigError

    with pytest.raises(ConfigError, match="ollama/llama3 becomes"):
        ModelSlot(provider="litellm")


@pytest.mark.parametrize("category", ["embedding", "rerank"])
def test_hosted_category_migration_is_valid_or_requires_manual_endpoint(category):
    from memspine.providers.registry import get_registry, validate_settings

    settings = {
        "model": "cohere/embed-model" if category == "embedding" else "cohere/rerank-model",
        "request_timeout_s": 4.0,
    }
    if category == "embedding":
        settings.update(dim=256, request_dimensions=True, query_input_type="search_query")
        original = {"model": {"embedding": {"provider": "litellm", "settings": settings}}}
        migrated = migrate(original)
        slot = migrated["model"]["embedding"]
        assert slot["provider"] == "anyllm"
        assert slot["settings"]["upstream_provider"] == "cohere"
        validate_settings(get_registry("embedders").get("anyllm"), slot["settings"])
        assert migrated == migrate(migrated)
    else:
        original = {"model": {"rerank": {"chain": [{"provider": "litellm", "settings": settings}]}}}
        with pytest.raises(ValueError, match="anyllm has no rerank API"):
            migrate(original)
        assert original["model"]["rerank"]["chain"][0]["provider"] == "litellm"
        settings["api_base"] = "https://cohere.example"
        migrated = migrate(original)
        slot = migrated["model"]["rerank"]["chain"][0]
        assert slot["provider"] == "openai_compatible" and slot["settings"]["family"] == "cohere"
        validate_settings(get_registry("rerankers").get(slot["provider"]), slot["settings"])
        assert migrated == migrate(migrated)


def test_rerank_ollama_is_never_written_as_an_unsupported_provider():
    with pytest.raises(ValueError, match="Ollama has no rerank API"):
        migrate(
            {
                "model": {
                    "rerank": {
                        "chain": [{"provider": "litellm", "settings": {"model": "ollama/ranker"}}]
                    }
                }
            }
        )


def test_cli_reports_manual_rerank_without_writing_invalid_config(tmp_path, monkeypatch, capsys):
    import json
    import sys

    from scripts.migrate_config_keys_adr085 import main

    path = tmp_path / "config.json"
    original = json.dumps(
        {
            "model": {
                "rerank": {
                    "chain": [
                        {
                            "provider": "litellm",
                            "settings": {"model": "cohere/rerank-v3", "request_timeout_s": 3.0},
                        }
                    ]
                }
            }
        }
    )
    path.write_text(original, encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["migrate", str(path)])
    assert main() == 2
    assert "manual:" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == original
