from __future__ import annotations

import pytest
from pydantic import ValidationError

from memspine.config.schema.providers import SqliteSettings
from memspine.providers.settings import (
    EmptySettings,
    LiteLLMRoleSettings,
    LlamaCppRoleSettings,
    Neo4jSettings,
    ProviderServicesSettings,
    StrictSettings,
    freeze_settings,
    freeze_settings_mapping,
)


def test_settings_reject_unknown_fields_and_coercion() -> None:
    with pytest.raises(ValidationError):
        EmptySettings.model_validate({"path": "./db.sqlite"})
    with pytest.raises(ValidationError):
        Neo4jSettings.model_validate(
            {
                "connect_timeout_s": "5",
                "request_timeout_s": 5.0,
            }
        )


def test_sqlite_busy_timeout_is_an_explicit_finite_setting() -> None:
    with pytest.raises(ValidationError):
        SqliteSettings.model_validate({"path": ":memory:"})
    settings = SqliteSettings.model_validate({"path": ":memory:", "busy_timeout_s": 0.25})
    assert settings.busy_timeout_s == 0.25
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            SqliteSettings.model_validate({"path": ":memory:", "busy_timeout_s": value})


def test_provider_timeouts_are_required_finite_and_allow_explicit_none_startup() -> None:
    with pytest.raises(ValidationError):
        ProviderServicesSettings.model_validate({"health_timeout_s": 5.0, "close_timeout_s": 10.0})
    settings = ProviderServicesSettings.model_validate(
        {"health_timeout_s": 5.0, "close_timeout_s": 10.0, "startup_timeout_s": None}
    )
    assert settings.startup_timeout_s is None
    for value in (0, -1, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            ProviderServicesSettings.model_validate(
                {"health_timeout_s": value, "close_timeout_s": 10.0, "startup_timeout_s": None}
            )


def test_enabled_llm_role_requires_an_explicit_positive_request_timeout() -> None:
    base = {"model": "openai/example", "api_key": "${secret:OPENAI_API_KEY}"}
    with pytest.raises(ValidationError):
        LiteLLMRoleSettings.model_validate(base)
    settings = LiteLLMRoleSettings.model_validate({**base, "timeout_seconds": 30.0})
    assert settings.timeout_seconds == 30.0
    assert (settings.temperature, settings.top_p, settings.max_tokens) == (0.2, 1.0, 2048)
    with pytest.raises(ValidationError):
        LiteLLMRoleSettings.model_validate({**base, "timeout_seconds": float("inf")})


def test_llm_adapters_have_distinct_native_generation_and_runtime_settings() -> None:
    llama = LlamaCppRoleSettings(model="llamacpp/model.gguf", timeout_seconds=4.0)
    assert (llama.temperature, llama.top_p, llama.max_tokens) == (0.8, 0.95, 2048)
    assert (llama.n_ctx, llama.n_gpu_layers, llama.n_batch, llama.n_threads) == (
        8192,
        0,
        512,
        None,
    )
    with pytest.raises(ValidationError):
        LlamaCppRoleSettings(model="openai/model", timeout_seconds=4.0)
    with pytest.raises(ValidationError):
        LiteLLMRoleSettings(
            model="openai/model",
            timeout_seconds=4.0,
            n_ctx=8192,
        )


def test_nested_settings_snapshots_are_recursively_immutable() -> None:
    source = {"limits": {"retries": 3}, "tags": ["a", "b"]}
    snapshot = freeze_settings_mapping(source)
    source["limits"]["retries"] = 9
    source["tags"].append("c")

    assert snapshot["limits"]["retries"] == 3  # type: ignore[index]
    assert snapshot["tags"] == ("a", "b")
    with pytest.raises(TypeError):
        snapshot["new"] = True  # type: ignore[index]
    with pytest.raises(ValueError, match="finite"):
        freeze_settings({"bad": float("nan")})
    with pytest.raises(TypeError):
        freeze_settings({"bad": object()})


def test_settings_base_is_strict_and_frozen() -> None:
    class NestedSettings(StrictSettings):
        values: dict[str, int]

    settings = NestedSettings.model_validate({"values": {"count": 2}})
    with pytest.raises(ValidationError):
        settings.values = {"count": 3}  # type: ignore[misc]
    with pytest.raises(TypeError, match="immutable"):
        settings.values["count"] = 3
