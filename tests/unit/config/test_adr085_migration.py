import pytest
from scripts.migrate_config_keys_adr085 import migrate


@pytest.mark.parametrize(
    ("route", "provider", "bare"),
    [
        ("ollama/llama3", "ollama", "llama3"),
        ("ollama_chat/qwen3", "ollama", "qwen3"),
        ("hosted_vllm/Qwen/Qwen3", "vllm", "Qwen/Qwen3"),
        ("bedrock/anthropic.x", "bedrock", "anthropic.x"),
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
