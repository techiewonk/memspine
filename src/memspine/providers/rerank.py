"""Rerank selection (E8, D-51): ``read.rerank`` mode -> descriptor -> built reranker.

"Multiple support" is one registry entry, not an engine if/elif. The descriptors live in
:mod:`memspine.providers.builtin.rerank` (the one rerank factory, RM-113); this module is the
selection layer the engine and the read pipeline call, so they never import a builtin factory.
Classified provider-unavailability errors return ``None`` so retrieval can continue without
reranking; configuration and programming errors remain visible to the caller.

``rerank_modes()`` is the single source the engine's startup validator reads, so a config typo
fails fast at start while the valid set stays in lock-step with the registry — including any
deployment-registered provider (``register_reranker``).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from memspine.exceptions import ConfigError, MissingServiceError
from memspine.providers.contexts import RerankerBuildContext
from memspine.providers.contracts import BuildResult
from memspine.providers.registry import (
    RERANKERS,
    ProviderDescriptor,
    ProviderRegistryError,
    get_registry,
    validate_settings,
)

if TYPE_CHECKING:
    from memspine.providers.shared import ResourceRegistry
    from memspine.services.embedding.static import StaticEmbedder
    from memspine.services.rerank.base import Reranker

__all__ = [
    "RerankSettings",
    "build_reranker",
    "build_reranker_result",
    "register_reranker",
    "rerank_modes",
    "static_prefilter_embedder",
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


def register_reranker(descriptor: ProviderDescriptor) -> None:
    """Register a typed deployment descriptor through the central registry."""
    get_registry("rerankers")
    if descriptor.ref.category != "rerank":
        raise ValueError("register_reranker requires a rerank descriptor")
    RERANKERS.register(descriptor.ref.name, descriptor, builtin=False, replace=True)


def rerank_modes() -> tuple[str, ...]:
    """The valid ``read.rerank`` values — ``off`` plus every registered provider.
    The ONE source for the engine's startup validation, so the check can never
    drift from what ``build_reranker`` actually supports (the A1 bug's root)."""
    get_registry("rerankers")
    return ("off", *RERANKERS.names())


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
    descriptor = RERANKERS.find(mode)
    if not isinstance(descriptor, ProviderDescriptor):
        raise ConfigError(
            f"unknown reranker provider {mode!r} (valid: {', '.join(rerank_modes())})"
        )
    if descriptor.requires_deployment_id and not explicit_deployment_id:
        raise ConfigError(f"runtime.deployment_id is required by shared provider rerank.{mode}")
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
        raise ConfigError(f"reranker builder identity mismatch for {settings.mode!r}")
    if settings.chunk_chars:
        from memspine.services.rerank.chunking import ChunkMaxReranker

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


def static_prefilter_embedder(registry: ResourceRegistry | None) -> StaticEmbedder:
    """The E4 static-embedding prefilter (default model) over the shared model2vec weights:
    the same registry key as a configured static embedder of the same model, so the prefilter
    never loads a second copy (RM-73). Raises ``MissingServiceError`` without ``[static]``."""
    from memspine.clients.weights.model2vec import static_weights
    from memspine.services.embedding.static import DEFAULT_MODEL, StaticEmbedder

    return StaticEmbedder(DEFAULT_MODEL, weights=static_weights(DEFAULT_MODEL, registry=registry))
