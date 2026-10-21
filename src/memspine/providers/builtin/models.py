"""Lazy built-in model provider factories registered as typed descriptors."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from memspine.config.schema.providers import (
    FastembedEmbeddingSettings,
    FastembedRerankerSettings,
    FlashrankRerankerSettings,
    HashEmbeddingSettings,
    JinaRerankerSettings,
    LiteLLMEmbeddingSettings,
    LiteLLMRerankerSettings,
    Qwen3RerankerSettings,
    SentenceTransformersEmbeddingSettings,
    StaticEmbeddingSettings,
)
from memspine.models.providers.llm.base import LLMService
from memspine.providers.contexts import LLMBuildContext, SharedBuildContext
from memspine.providers.contracts import (
    BuildResult,
    Category,
    DependencyRequirement,
    ProviderRef,
)
from memspine.providers.registry import EMBEDDERS, LLM_PROVIDERS, RERANKERS, ProviderDescriptor
from memspine.providers.settings import (
    LiteLLMRoleSettings,
    LlamaCppRoleSettings,
    StrictSettings,
)
from memspine.providers.shared import ResourceRegistry, shared_registry
from memspine.services.base import CapabilityManifest

__all__ = ["register"]


class RerankerSettings(StrictSettings):
    model: str | None = None
    api_base: str | None = None
    api_key: str | None = None
    aws_region: str | None = None
    instruction: str | None = None
    device: str | None = None
    quant: str | None = None


@dataclass(frozen=True, slots=True)
class _RerankerBuilder:
    ref: ProviderRef
    factory_name: str
    capabilities: frozenset[str] = frozenset({"rerank"})

    def construct(
        self, settings: Any, ctx: Any = None, registry: ResourceRegistry | None = None
    ) -> Any:
        from memspine.models.providers.rerank import factory as rerank_factory

        current = rerank_factory.RerankSettings(
            mode=self.ref.name,
            model=getattr(settings, "model", None),
            api_base=getattr(settings, "api_base", None),
            api_key=getattr(settings, "api_key", None),
            aws_region=getattr(settings, "aws_region", None),
            instruction=getattr(settings, "instruction", None),
            device=getattr(settings, "device", None),
            quant=getattr(settings, "quant", None),
            request_timeout_s=getattr(settings, "request_timeout_s", None),
            batch_size=getattr(settings, "batch_size", None),
            max_length=getattr(settings, "max_length", None),
            max_documents=getattr(settings, "max_documents", None),
            chunk_chars=getattr(ctx, "chunk_chars", 0) if ctx is not None else 0,
            chunk_overlap=getattr(ctx, "chunk_overlap", 0) if ctx is not None else 0,
        )
        return getattr(rerank_factory, self.factory_name)(current, registry)

    async def build(self, settings: Any, ctx: Any) -> BuildResult[Any]:
        shared = getattr(ctx, "shared", None)
        if not isinstance(shared, SharedBuildContext):
            # Standalone build (no Engine lifecycle): private weights, freed with the object.
            return BuildResult(self.construct(settings, ctx), self.ref, (), self.capabilities)
        # Engine build: weights come from the process registry; this Engine's lease is
        # released by its lifecycle, never by another Engine (ADR-079 / RM-73).
        service = self.construct(settings, ctx, shared_registry())
        release = getattr(service, "aclose", None)
        refs = (
            (shared.resources.own(f"reranker.weights:{self.ref.name}", release),)
            if callable(release)
            else ()
        )
        return BuildResult(service, self.ref, refs, self.capabilities)


@dataclass(frozen=True, slots=True)
class _SyncBuilder:
    ref: ProviderRef
    factory: Callable[[Any, Any], Any]
    capabilities: frozenset[str]

    async def build(self, settings: Any, ctx: Any) -> BuildResult[Any]:
        service = self.factory(settings, ctx)
        return BuildResult(service, self.ref, (), self.capabilities)


@dataclass(frozen=True, slots=True)
class _EmbeddingBuilder:
    """Local embedders take the process weight registry when an Engine builds them, and the
    Engine lifecycle releases its lease; the result cache stays per Engine (CachedEmbedding)."""

    ref: ProviderRef
    factory: Callable[[Any, ResourceRegistry | None], Any]
    capabilities: frozenset[str]

    async def build(self, settings: Any, ctx: Any) -> BuildResult[Any]:
        if not isinstance(ctx, SharedBuildContext):
            return BuildResult(self.factory(settings, None), self.ref, (), self.capabilities)
        service = self.factory(settings, shared_registry())
        release = getattr(service, "aclose", None)
        refs = (
            (ctx.resources.own(f"embedding.weights:{self.ref.name}", release),)
            if callable(release)
            else ()
        )
        return BuildResult(service, self.ref, refs, self.capabilities)


@dataclass(frozen=True, slots=True)
class _LlamaCppBuilder:
    ref: ProviderRef

    async def build(
        self, settings: LlamaCppRoleSettings, ctx: LLMBuildContext
    ) -> BuildResult[LLMService]:
        from memspine.exceptions import ConfigError
        from memspine.models.providers.llm.llama_cpp import LlamaCppLLM

        prefix = "llamacpp/"
        if not settings.model.startswith(prefix) or not settings.model[len(prefix) :]:
            raise ConfigError("llamacpp provider model must have the form 'llamacpp/<model-path>'")

        service = LlamaCppLLM.allocate()
        # The lifecycle id stays per role/profile (one holder each); the native model is keyed
        # by path and load options, so roles/profiles on the same GGUF share one load.
        resource = ctx.shared.resources.own(ctx.resource_key or f"llm:{ctx.role}", service.close)
        native_options: dict[str, Any] = {
            "n_gpu_layers": settings.n_gpu_layers,
            "n_batch": settings.n_batch,
        }
        if settings.n_threads is not None:
            native_options["n_threads"] = settings.n_threads
        try:
            await service.initialize_shared(
                settings.model[len(prefix) :],
                registry=shared_registry(),
                n_ctx=settings.n_ctx,
                timeout_seconds=settings.timeout_seconds,
                temperature=settings.temperature,
                top_p=settings.top_p,
                max_tokens=settings.max_tokens,
                no_think=settings.no_think,
                **native_options,
            )
        except BaseException:
            # The registered closer handles a constructor that fails before it
            # returns a native handle. If it did return one, close it immediately.
            await service.close()
            raise
        return BuildResult(service, self.ref, (resource,), frozenset({"llm"}))


def _embed_fast(settings: FastembedEmbeddingSettings, registry: ResourceRegistry | None) -> Any:
    if settings.dim is None:
        from memspine.exceptions import ConfigError

        raise ConfigError("embedding.settings.dim could not be resolved")
    from memspine.models.encoders.embedding.fastembed_local import FastembedEmbedding

    return FastembedEmbedding(
        model=settings.model,
        query_instruction=settings.query_instruction,
        dim=settings.dim,
        registry=registry,
    )


def _embed_hash(settings: HashEmbeddingSettings, _registry: ResourceRegistry | None) -> Any:
    from memspine.models.encoders.embedding.hash_local import HashEmbedding

    return HashEmbedding(dim=settings.dim)


def _embed_static(settings: StaticEmbeddingSettings, registry: ResourceRegistry | None) -> Any:
    if settings.dim is None:
        from memspine.exceptions import ConfigError

        raise ConfigError("embedding.settings.dim could not be resolved")
    from memspine.models.encoders.embedding.static_local import StaticEmbedder

    return StaticEmbedder(model=settings.model, dim=settings.dim, registry=registry)


def _embed_litellm(settings: LiteLLMEmbeddingSettings, _registry: ResourceRegistry | None) -> Any:
    if settings.dim is None:
        from memspine.exceptions import ConfigError

        raise ConfigError("embedding.settings.dim is required when embedding.provider='litellm'")
    from memspine.models.encoders.embedding.litellm_embed import LiteLLMEmbedding

    return LiteLLMEmbedding(
        settings.model,
        settings.dim,
        api_base=settings.api_base,
        api_key=settings.api_key,
        aws_region=settings.aws_region,
        request_timeout_s=settings.request_timeout_s,
        request_dimensions=settings.request_dimensions,
        query_input_type=settings.query_input_type,
        document_input_type=settings.document_input_type,
    )


def _embed_st(
    settings: SentenceTransformersEmbeddingSettings, registry: ResourceRegistry | None
) -> Any:
    if settings.dim is None:
        from memspine.exceptions import ConfigError

        raise ConfigError("embedding.settings.dim is required when embedding.provider='st'")
    from memspine.models.encoders.embedding.st_local import SentenceTransformersEmbedding

    return SentenceTransformersEmbedding(
        settings.model,
        settings.dim,
        query_instruction=settings.query_instruction,
        device=settings.device,
        dtype=settings.dtype,
        query_prompt_name=settings.query_prompt_name,
        document_prompt_name=settings.document_prompt_name,
        trust_remote_code=settings.trust_remote_code,
        registry=registry,
    )


def _register(
    registry: Any,
    category: Category,
    name: str,
    settings_model: type[BaseModel],
    factory: Callable[[Any, Any], Any],
    capability: str,
    *,
    requires_deployment_id: bool = False,
    requirements: tuple[DependencyRequirement, ...] = (),
    extra: str | None = None,
) -> None:
    ref = ProviderRef(category, name)
    registry.register(
        name,
        ProviderDescriptor(
            ref=ref,
            settings_model=settings_model,
            declared_capabilities=frozenset({capability}),
            requirements=requirements,
            builder=(
                _EmbeddingBuilder(ref, factory, frozenset({capability}))
                if category == "embedding"
                else _SyncBuilder(ref, factory, frozenset({capability}))
            ),
            service_manifest=CapabilityManifest(
                name, capability, provides=(capability,), extra=extra
            ),
            requires_deployment_id=requires_deployment_id,
            target_identity=(_embedding_target_identity(name) if category == "embedding" else None),
        ),
    )


def _embedding_target_identity(name: str) -> Callable[[Any], dict[str, Any]]:
    """Expose a no-load embedding table identity for pre-connect projection checks."""
    if name == "fastembed":
        from memspine.models.encoders.embedding.fastembed_local import _KNOWN_DIMS

        def fastembed_identity(settings: FastembedEmbeddingSettings) -> dict[str, Any]:
            dimension = _KNOWN_DIMS.get(settings.model, settings.dim)
            if dimension is None:
                from memspine.exceptions import ConfigError

                raise ConfigError(
                    "embedding.settings.dim is required to identify an unknown fastembed model"
                )
            if settings.dim is not None and settings.dim != dimension:
                from memspine.exceptions import ConfigError

                raise ConfigError(
                    "embedding.settings.dim conflicts with the known fastembed model dimension"
                )
            return {"embedder_id": f"fastembed:{settings.model}", "dimension": dimension}

        return fastembed_identity
    if name == "hash":
        return lambda settings: {
            "embedder_id": f"hash:{settings.dim}",
            "dimension": settings.dim,
        }
    if name == "static":
        return lambda settings: {
            "embedder_id": f"static:{settings.model}",
            "dimension": settings.dim,
        }
    if name == "litellm":
        return lambda settings: {
            "embedder_id": (
                f"litellm:{settings.model}"
                + (f"@{settings.dim}" if settings.request_dimensions else "")
                + (
                    f"[{settings.document_input_type}|{settings.query_input_type}]"
                    if settings.query_input_type or settings.document_input_type
                    else ""
                )
            ),
            "dimension": settings.dim,
        }
    if name == "st":
        return lambda settings: {"embedder_id": f"st:{settings.model}", "dimension": settings.dim}
    raise ValueError(f"no embedding identity implementation for builtin {name!r}")


def register() -> None:
    """Register lazy descriptors; importing this module loads no model adapter."""
    models: tuple[
        tuple[
            Any,
            Category,
            str,
            type[BaseModel],
            Callable[[Any, Any], Any],
            str,
            bool,
            tuple[DependencyRequirement, ...],
            str | None,
        ],
        ...,
    ] = (
        (
            LLM_PROVIDERS,
            "llm",
            "litellm",
            LiteLLMRoleSettings,
            lambda settings, _ctx: _litellm(settings),
            "llm",
            True,
            (),
            None,
        ),
        (
            EMBEDDERS,
            "embedding",
            "fastembed",
            FastembedEmbeddingSettings,
            _embed_fast,
            "embedding",
            False,
            (),
            None,
        ),
        (
            EMBEDDERS,
            "embedding",
            "hash",
            HashEmbeddingSettings,
            _embed_hash,
            "embedding",
            False,
            (),
            None,
        ),
        (
            EMBEDDERS,
            "embedding",
            "static",
            StaticEmbeddingSettings,
            _embed_static,
            "embedding",
            False,
            (DependencyRequirement("package", "model2vec"),),
            "static",
        ),
        (
            EMBEDDERS,
            "embedding",
            "litellm",
            LiteLLMEmbeddingSettings,
            _embed_litellm,
            "embedding",
            True,
            (),
            None,
        ),
        (
            EMBEDDERS,
            "embedding",
            "st",
            SentenceTransformersEmbeddingSettings,
            _embed_st,
            "embedding",
            False,
            (DependencyRequirement("package", "sentence_transformers"),),
            "st",
        ),
    )
    for (
        registry,
        category,
        name,
        settings_model,
        factory,
        capability,
        deployment_id,
        requirements,
        extra,
    ) in models:
        if name not in registry:
            _register(
                registry,
                category,
                name,
                settings_model,
                factory,
                capability,
                requires_deployment_id=deployment_id,
                requirements=requirements,
                extra=extra,
            )

    if "llamacpp" not in LLM_PROVIDERS:
        ref = ProviderRef("llm", "llamacpp")
        LLM_PROVIDERS.register(
            ref.name,
            ProviderDescriptor(
                ref=ref,
                settings_model=LlamaCppRoleSettings,
                declared_capabilities=frozenset({"llm"}),
                requirements=(DependencyRequirement("package", "llama_cpp"),),
                builder=_LlamaCppBuilder(ref),
                service_manifest=CapabilityManifest(
                    "llamacpp", "llm", provides=("llm",), extra="llmlocal"
                ),
            ),
        )

    rerankers: tuple[
        tuple[
            str,
            type[BaseModel],
            str,
            tuple[DependencyRequirement, ...],
            str | None,
        ],
        ...,
    ] = (
        ("fastembed", FastembedRerankerSettings, "_build_fastembed", (), None),
        (
            "flashrank",
            FlashrankRerankerSettings,
            "_build_flashrank",
            (DependencyRequirement("package", "flashrank"),),
            "rerank",
        ),
        ("litellm", LiteLLMRerankerSettings, "_build_litellm", (), None),
        (
            "qwen3",
            Qwen3RerankerSettings,
            "_build_qwen3",
            (
                DependencyRequirement("package", "transformers"),
                DependencyRequirement("package", "torch"),
            ),
            "st",
        ),
        (
            "jina",
            JinaRerankerSettings,
            "_build_jina",
            (
                DependencyRequirement("package", "transformers"),
                DependencyRequirement("package", "torch"),
            ),
            "st",
        ),
    )
    for name, settings_model, factory_name, requirements, extra in rerankers:
        if name not in RERANKERS:
            ref = ProviderRef("reranker", name)
            RERANKERS.register(
                name,
                ProviderDescriptor(
                    ref=ref,
                    settings_model=settings_model,
                    declared_capabilities=frozenset({"rerank"}),
                    requirements=requirements,
                    builder=_RerankerBuilder(ref, factory_name),
                    service_manifest=CapabilityManifest(
                        name, "rerank", provides=("rerank",), extra=extra
                    ),
                    requires_deployment_id=name == "litellm",
                ),
            )


def _litellm(settings: LiteLLMRoleSettings) -> Any:
    from memspine.models.providers.llm.litellm_llm import LiteLLMLLM

    return LiteLLMLLM(
        settings.model,
        api_base=settings.api_base,
        api_key=(settings.api_key.get_secret_value() if settings.api_key is not None else None),
        aws_region=settings.aws_region,
        timeout_seconds=settings.timeout_seconds,
        no_think=settings.no_think,
        temperature=settings.temperature,
        top_p=settings.top_p,
        max_tokens=settings.max_tokens,
    )
