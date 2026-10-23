"""Strict settings contracts for provider construction and runtime operations."""

from __future__ import annotations

import math
from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

type FrozenJsonValue = (
    bool
    | int
    | float
    | str
    | tuple["FrozenJsonValue", ...]
    | Mapping[str, "FrozenJsonValue"]
    | None
)


class FrozenDict(dict[str, object]):
    """A dict-compatible read-only container for frozen Pydantic settings."""

    def __setitem__(self, key: str, value: object) -> None:
        raise TypeError("provider settings are immutable")

    def __delitem__(self, key: str) -> None:
        raise TypeError("provider settings are immutable")

    def clear(self) -> None:
        raise TypeError("provider settings are immutable")

    def pop(self, key: str, *default: object) -> object:
        raise TypeError("provider settings are immutable")

    def popitem(self) -> tuple[str, object]:
        raise TypeError("provider settings are immutable")

    def setdefault(self, key: str, default: object = None) -> object:
        raise TypeError("provider settings are immutable")

    def update(self, *args: object, **kwargs: object) -> None:
        raise TypeError("provider settings are immutable")

    def __ior__(self, other: Mapping[str, object]) -> FrozenDict:  # type: ignore[override,misc]
        raise TypeError("provider settings are immutable")


def _freeze_model_value(value: object) -> object:
    if isinstance(value, dict):
        return FrozenDict({key: _freeze_model_value(nested) for key, nested in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_model_value(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze_model_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_model_value(item) for item in value)
    return value


class StrictSettings(BaseModel):
    """Base for selected provider settings; coercion and unknown keys are rejected."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        allow_inf_nan=False,
    )

    def model_post_init(self, __context: object) -> None:
        # frozen=True blocks attribute replacement; freeze nested containers too.
        for field_name, value in self.__dict__.items():
            object.__setattr__(self, field_name, _freeze_model_value(value))


class EmptySettings(StrictSettings):
    """Settings model for providers with no construction options."""


class Neo4jSettings(StrictSettings):
    """Direct Neo4j connection settings; deployment ownership comes from runtime."""

    url_env: str = Field(default="MEMSPINE_NEO4J_URL", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    user_env: str = Field(default="MEMSPINE_NEO4J_USER", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    password_env: str = Field(
        default="MEMSPINE_NEO4J_PASSWORD", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$"
    )
    database: str = Field(default="neo4j", min_length=1)
    connect_timeout_s: float = Field(gt=0)
    request_timeout_s: float = Field(gt=0)


class _LLMGenerationSettings(StrictSettings):
    """Shared type shapes; each adapter declares its own values and defaults."""

    model: str = Field(min_length=1)
    timeout_seconds: float = Field(gt=0)
    temperature: float = Field(default=0.2, ge=0.0, le=2.0, allow_inf_nan=False)
    top_p: float = Field(default=1.0, gt=0.0, le=1.0, allow_inf_nan=False)
    max_tokens: int = Field(default=2048, ge=1)


class OpenAICompatRoleSettings(_LLMGenerationSettings):
    """Native settings for one OpenAICompat-backed role."""

    api_base: str | None = None
    api_key: SecretStr | None = None
    api_key_env: str | None = None
    supports_json_schema: bool = True
    supports_grammar: bool = False
    no_think: bool | None = None


class AnyLLMRoleSettings(_LLMGenerationSettings):
    upstream_provider: str = Field(min_length=1)
    api_key_env: str | None = None
    api_base: str | None = None
    no_think: bool | None = None
    supports_json_schema: bool = False


def _bare_model(model: str, routes: tuple[str, ...], provider: str) -> str:
    """ADR-068: a model-name prefix never selects an adapter, so the route belongs to the
    descriptor; a model id that repeats it is rejected with a pointer."""
    for route in routes:
        if model.startswith(route):
            raise ValueError(
                f"{provider} settings.model is the bare model id: drop the {route!r} prefix "
                f"(the {provider} provider adds its own route)"
            )
    return model


class OllamaRoleSettings(_LLMGenerationSettings):
    """RM-113: an Ollama server through the OpenAICompat connector (route ``ollama_chat/``)."""

    api_base: str = Field(default="http://localhost:11434", min_length=1)
    no_think: bool | None = None

    @field_validator("model")
    @classmethod
    def _bare(cls, value: str) -> str:
        return _bare_model(value, ("ollama/", "ollama_chat/"), "ollama")


class VllmRoleSettings(_LLMGenerationSettings):
    """RM-113: a vLLM OpenAI-compatible server through the OpenAICompat connector
    (route ``hosted_vllm/``). ``api_base`` is the server URL (e.g. ``http://host:8000/v1``)."""

    api_base: str = Field(min_length=1)
    api_key: SecretStr | None = None
    no_think: bool | None = None

    @field_validator("model")
    @classmethod
    def _bare(cls, value: str) -> str:
        return _bare_model(value, ("hosted_vllm/", "openai/"), "vllm")


class BedrockRoleSettings(_LLMGenerationSettings):
    """RM-113: AWS Bedrock through the OpenAICompat connector (route ``bedrock/``); credentials
    come from the boto3 credential chain, ``aws_region`` selects the region."""

    aws_region: str | None = None
    no_think: bool | None = None

    @field_validator("model")
    @classmethod
    def _bare(cls, value: str) -> str:
        return _bare_model(value, ("bedrock/",), "bedrock")


class LlamaCppRoleSettings(_LLMGenerationSettings):
    """Native settings for one in-process llama.cpp role."""

    model: str = Field(min_length=len("llamacpp/x"), pattern=r"^llamacpp/.+$")
    temperature: float = Field(default=0.8, ge=0.0, le=2.0, allow_inf_nan=False)
    top_p: float = Field(default=0.95, gt=0.0, le=1.0, allow_inf_nan=False)
    n_ctx: int = Field(default=8192, ge=1)
    n_gpu_layers: int = Field(default=0, ge=-1)
    n_batch: int = Field(default=512, ge=1)
    n_threads: int | None = Field(default=None, ge=1)
    no_think: bool | None = None


class ProviderServicesSettings(StrictSettings):
    """Required provider lifecycle timeouts. None startup timeout is explicit."""

    health_timeout_s: float = Field(gt=0)
    close_timeout_s: float = Field(gt=0)
    startup_timeout_s: float | None = Field(gt=0)


class ProjectionSwitchSettings(StrictSettings):
    """Controlled projection switch mode and owned-generation retention/sweep policy."""

    mode: Literal["controlled"] = "controlled"
    retention_s: int | None = Field(default=None, ge=0)
    # None keeps automatic cleanup disabled. Setting an interval opts into an
    # immediate bounded startup sweep followed by periodic bounded sweeps.
    cleanup_interval_s: float | None = Field(default=None, gt=0, strict=True, allow_inf_nan=False)
    cleanup_batch_size: int = Field(default=50, ge=1, strict=True)


def freeze_settings(value: object, *, path: str = "settings") -> FrozenJsonValue:
    """Validate JSON-like settings and recursively freeze every container.

    This is for resolved snapshots and diagnostics-safe transport values. Secrets
    should be represented by references and resolved separately at build time.
    """
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{path} must contain only finite numbers")
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, FrozenJsonValue] = {}
        for key, nested in value.items():
            if not isinstance(key, str):
                raise TypeError(f"{path} keys must be strings")
            frozen[key] = freeze_settings(nested, path=f"{path}.{key}")
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(
            freeze_settings(item, path=f"{path}[{index}]") for index, item in enumerate(value)
        )
    raise TypeError(f"{path} must contain JSON-compatible values")


def freeze_settings_mapping(value: Mapping[str, object]) -> Mapping[str, FrozenJsonValue]:
    """Freeze a top-level settings mapping into a read-only snapshot."""
    frozen = freeze_settings(value)
    if not isinstance(frozen, Mapping):
        raise TypeError("settings must be an object")
    return frozen
