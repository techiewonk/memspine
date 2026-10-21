"""Reranker factory (E8, D-51): a registry mapping a rerank mode to a lazy
constructor, so "multiple support" is one registry entry — not an engine
if/elif. Each spec imports its adapter inside the builder (nothing heavy loads at
module import). Classified provider-unavailability errors return ``None`` so
retrieval can continue without reranking; configuration and programming errors
remain visible to the caller.

``rerank_modes()`` is the single source the engine's startup validator reads, so
a config typo fails fast at start while the valid set stays in lock-step with
the registry — including any deployment-registered provider (``register_reranker``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any

from memspine.exceptions import MissingServiceError
from memspine.models.providers.rerank.base import Reranker
from memspine.providers.builtin.models import register as _register_builtin_models
from memspine.providers.contexts import RerankerBuildContext
from memspine.providers.contracts import BuildResult
from memspine.providers.registry import (
    RERANKERS,
    ProviderDescriptor,
    ProviderRegistryError,
    get_registry,
    validate_settings,
)
from memspine.providers.shared import ResourceRegistry

__all__ = [
    "RerankSettings",
    "build_reranker",
    "build_reranker_result",
    "register_reranker",
    "rerank_modes",
    "validate_reranker_settings",
]


@dataclass(frozen=True)
class RerankSettings:
    """The resolved rerank config a spec needs (from ``read.rerank*``)."""

    mode: str
    model: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None
    request_timeout_s: float | None = None
    #: N12 (plan v3.2): the task instruction of an instruction-conditioned reranker
    #: (``qwen3``); None keeps its default memory instruction.
    instruction: str | None = None
    #: ``qwen3``: torch device and bitsandbytes quantisation ("4bit" | "8bit").
    device: str | None = None
    quant: str | None = None
    batch_size: int | None = None
    max_length: int | None = None
    max_documents: int | None = None
    #: I9 (opt-in): documents longer than this many characters are scored in
    #: overlapping windows and take the max window score. None = unchanged.
    chunk_chars: int | None = None
    chunk_overlap: int = 0


# Each local builder takes the weight registry: the process registry for an Engine-owned build
# (weights shared per process, ADR-079 / RM-73), None for a standalone private load.


def _build_fastembed(
    settings: RerankSettings, registry: ResourceRegistry | None = None
) -> Reranker:
    from memspine.models.providers.rerank.fastembed_rerank import FastembedReranker

    # FastembedReranker takes a required str with a default model; pass through
    # only when configured, else let it pick its default.
    if settings.model:
        return FastembedReranker(settings.model, registry=registry)
    return FastembedReranker(registry=registry)


def _build_flashrank(
    settings: RerankSettings, registry: ResourceRegistry | None = None
) -> Reranker:
    from memspine.models.providers.rerank.flashrank_rerank import FlashrankReranker

    return FlashrankReranker(settings.model, registry=registry)


def _build_litellm(settings: RerankSettings, registry: ResourceRegistry | None = None) -> Reranker:
    del registry  # remote provider: no local weights
    if not settings.model:
        # An explicitly selected provider with missing model settings is a config error.
        raise MissingServiceError(
            "services.rerank.litellm (set read.rerank_settings.model)", extra="litellm"
        )
    if settings.request_timeout_s is None:
        from memspine.exceptions import ConfigError

        raise ConfigError(
            "read.rerank_settings.request_timeout_s is required for the litellm reranker"
        )
    from memspine.models.providers.rerank.litellm_rerank import LiteLLMReranker

    return LiteLLMReranker(
        settings.model,
        api_base=settings.api_base,
        api_key=settings.api_key,
        aws_region=settings.aws_region,
        request_timeout_s=settings.request_timeout_s,
    )


def _build_qwen3(settings: RerankSettings, registry: ResourceRegistry | None = None) -> Reranker:
    from memspine.models.providers.rerank.qwen3_rerank import (
        DEFAULT_INSTRUCTION,
        DEFAULT_MODEL,
        Qwen3Reranker,
    )

    return Qwen3Reranker(
        settings.model or DEFAULT_MODEL,
        instruction=settings.instruction or DEFAULT_INSTRUCTION,
        device=settings.device,
        quant=settings.quant,
        batch_size=settings.batch_size if settings.batch_size is not None else 8,
        max_length=settings.max_length if settings.max_length is not None else 8192,
        registry=registry,
    )


def _build_jina(settings: RerankSettings, registry: ResourceRegistry | None = None) -> Reranker:
    from memspine.models.providers.rerank.jina_rerank import DEFAULT_MODEL, JinaReranker

    return JinaReranker(
        settings.model or DEFAULT_MODEL,
        device=settings.device,
        max_documents=settings.max_documents if settings.max_documents is not None else 64,
        registry=registry,
    )


_REGISTRY = RERANKERS


def register_reranker(descriptor: ProviderDescriptor) -> None:
    """Register a typed deployment descriptor through the central registry."""
    _register_builtin_models()
    if descriptor.ref.category != "reranker":
        raise ValueError("register_reranker requires a reranker descriptor")
    _REGISTRY.register(descriptor.ref.name, descriptor, builtin=False, replace=True)


def rerank_modes() -> tuple[str, ...]:
    """The valid ``read.rerank`` values — ``off`` plus every registered provider.
    The ONE source for the engine's startup validation, so the check can never
    drift from what ``build_reranker`` actually supports (the A1 bug's root)."""
    get_registry("rerankers")
    return ("off", *_REGISTRY.names())


def _settings_values(settings: RerankSettings) -> dict[str, Any]:
    return {
        key: getattr(settings, key)
        for key in (
            "model",
            "api_base",
            "api_key",
            "aws_region",
            "instruction",
            "device",
            "quant",
            "request_timeout_s",
            "batch_size",
            "max_length",
            "max_documents",
        )
        if getattr(settings, key, None) is not None
    }


def validate_reranker_settings(
    mode: str,
    settings: Mapping[str, Any],
    *,
    explicit_deployment_id: bool,
    allow_irrelevant: bool = False,
) -> tuple[ProviderDescriptor, Any] | None:
    """Validate options and shared-provider requirements without loading a model."""
    if mode == "off":
        return None
    get_registry("rerankers")
    descriptor = _REGISTRY.find(mode)
    if not isinstance(descriptor, ProviderDescriptor):
        from memspine.exceptions import ConfigError

        raise ConfigError(
            f"unknown reranker provider {mode!r} (valid: {', '.join(rerank_modes())})"
        )
    if descriptor.requires_deployment_id and not explicit_deployment_id:
        from memspine.exceptions import ConfigError

        raise ConfigError(f"runtime.deployment_id is required by shared provider reranker.{mode}")
    values = dict(settings)
    if allow_irrelevant:
        values = {
            key: value
            for key, value in values.items()
            if key in descriptor.settings_model.model_fields
        }
    try:
        typed = validate_settings(descriptor, values)
    except ProviderRegistryError as exc:
        from memspine.exceptions import ConfigError

        raise ConfigError(
            f"invalid read.rerank_settings for provider {mode!r} ({type(exc).__name__})"
        ) from None
    return descriptor, typed


async def build_reranker_result(
    settings: RerankSettings,
    context: RerankerBuildContext | None = None,
    *,
    explicit_deployment_id: bool = False,
) -> BuildResult[Reranker] | None:
    """Build through the selected async descriptor and preserve result metadata."""
    if settings.mode == "off":
        return None
    validated = validate_reranker_settings(
        settings.mode,
        _settings_values(settings),
        explicit_deployment_id=explicit_deployment_id,
    )
    assert validated is not None
    descriptor, typed = validated
    try:
        built = await descriptor.builder.build(typed, context)
    except (MissingServiceError, ConnectionError, TimeoutError):
        # These failures mean the provider cannot currently serve this mode;
        # configuration and programming errors remain visible to the caller.
        return None
    if built.provider != descriptor.ref:
        from memspine.exceptions import ConfigError

        raise ConfigError(f"reranker builder identity mismatch for {settings.mode!r}")
    if settings.chunk_chars:
        from memspine.models.providers.rerank.chunking import ChunkMaxReranker

        built = replace(
            built,
            service=ChunkMaxReranker(built.service, settings.chunk_chars, settings.chunk_overlap),
        )
    return built


async def build_reranker(
    settings: RerankSettings,
    context: RerankerBuildContext | None = None,
    *,
    explicit_deployment_id: bool = False,
) -> Reranker | None:
    """Build the selected reranker asynchronously through its authoritative descriptor."""
    result = await build_reranker_result(
        settings, context, explicit_deployment_id=explicit_deployment_id
    )
    return None if result is None else result.service
