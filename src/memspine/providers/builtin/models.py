"""Built-in LLM and embedding descriptors (slots ``model.llm`` and ``model.embedding``).

Each builder maps typed settings to a connector (``clients.weights.<lib>`` for local weights,
``clients.openai_compat`` for remote endpoints) and injects it into the adapter (RM-113). Local
weights come from the process registry when an Engine builds them (ADR-079 / RM-73) and the
Engine lifecycle releases its lease. Rerank descriptors live in ``builtin/rerank.py``,
extraction in ``builtin/extraction.py`` and deciders in ``builtin/decider.py``.

The ``ollama``, ``vllm`` and ``bedrock`` LLM providers are their own descriptors over the
OpenAICompat connector; the route (``ollama_chat/``, ``hosted_vllm/``, ``bedrock/``) is fixed by
the descriptor, never inferred from a model-name prefix (ADR-068).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from memspine.config.schema.providers import (
    HashEmbeddingSettings,
    OpenAICompatEmbeddingSettings,
    OnnxEmbeddingSettings,
    SentenceTransformersEmbeddingSettings,
    StaticEmbeddingSettings,
)
from memspine.providers.contexts import LLMBuildContext, SharedBuildContext
from memspine.providers.contracts import (
    BuildResult,
    Category,
    DependencyRequirement,
    ProviderRef,
)
from memspine.providers.registry import EMBEDDERS, LLM_PROVIDERS, ProviderDescriptor
from memspine.providers.settings import (
    AnyLLMRoleSettings,
    BedrockRoleSettings,
    LlamaCppRoleSettings,
    OllamaRoleSettings,
    OpenAICompatRoleSettings,
    VllmRoleSettings,
)
from memspine.providers.shared import ResourceRegistry
from memspine.services.base import CapabilityManifest
from memspine.services.llm.base import LLMService

__all__ = ["register"]


@dataclass(frozen=True, slots=True)
class _SyncBuilder:
    ref: ProviderRef
    factory: Callable[[Any, Any], Any]
    capabilities: frozenset[str]

    async def build(self, settings: Any, ctx: Any) -> BuildResult[Any]:
        service = self.factory(settings, ctx)
        close = getattr(service, "aclose", None)
        refs = ()
        if isinstance(ctx, LLMBuildContext) and callable(close):
            refs = (ctx.shared.resources.own(ctx.resource_key or f"llm:{ctx.role}", close),)
        return BuildResult(service, self.ref, refs, self.capabilities)


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
        service = self.factory(settings, ctx.resource_registry())
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
        from memspine.clients.weights.llama_cpp import LlamaCppSpec, llama_weights
        from memspine.exceptions import ConfigError
        from memspine.services.llm.llamacpp import LlamaCppLLM

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
        model_path = settings.model[len(prefix) :]
        try:
            weights = llama_weights(
                LlamaCppSpec(model_path, settings.n_ctx, native_options),
                registry=ctx.shared.resource_registry(),
            )
            await service.initialize_shared(
                model_path,
                weights=weights,
                timeout_seconds=settings.timeout_seconds,
                temperature=settings.temperature,
                top_p=settings.top_p,
                max_tokens=settings.max_tokens,
                no_think=settings.no_think,
            )
        except BaseException:
            # The registered closer handles a constructor that fails before it
            # returns a native handle. If it did return one, close it immediately.
            await service.close()
            raise
        return BuildResult(service, self.ref, (resource,), frozenset({"llm"}))


# -- embedding ---------------------------------------------------------------------------------


def _missing_dim(provider: str | None = None) -> Exception:
    from memspine.exceptions import ConfigError

    if provider is None:
        return ConfigError("model.embedding.settings.dim could not be resolved")
    return ConfigError(
        f"model.embedding.settings.dim is required when model.embedding.provider='{provider}'"
    )


def _embed_onnx(settings: OnnxEmbeddingSettings, registry: ResourceRegistry | None) -> Any:
    if settings.dim is None:
        raise _missing_dim()
    from memspine.clients.weights.onnx import text_weights
    from memspine.services.embedding.onnx import OnnxEmbedding

    return OnnxEmbedding(
        model=settings.model,
        query_instruction=settings.query_instruction,
        dim=settings.dim,
        batch_size=settings.batch_size,
        model_spec=settings.model_spec,
        weights=text_weights(
            settings.model, model_spec=settings.model_spec, threads=settings.threads,
            providers=settings.providers, local_files_only=settings.local_files_only,
            registry=registry,
        ),
    )


def _embed_hash(settings: HashEmbeddingSettings, _registry: ResourceRegistry | None) -> Any:
    from memspine.services.embedding.hash import HashEmbedding

    return HashEmbedding(dim=settings.dim)


def _embed_static(settings: StaticEmbeddingSettings, registry: ResourceRegistry | None) -> Any:
    if settings.dim is None:
        raise _missing_dim()
    from memspine.clients.weights.model2vec import static_weights
    from memspine.services.embedding.static import StaticEmbedder

    return StaticEmbedder(
        model=settings.model,
        dim=settings.dim,
        local_files_only=settings.local_files_only,
        weights=static_weights(
            settings.model, local_files_only=settings.local_files_only, registry=registry
        ),
    )


def _embed_openai_compat(
    settings: OpenAICompatEmbeddingSettings, _registry: ResourceRegistry | None
) -> Any:
    if settings.dim is None:
        raise _missing_dim("openai_compatible")
    from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec
    from memspine.services.embedding.openai_compat import OpenAICompatEmbedding

    return OpenAICompatEmbedding(
        settings.model,
        settings.dim,
        request_timeout_s=settings.request_timeout_s,
        request_dimensions=settings.request_dimensions,
        query_input_type=settings.query_input_type,
        document_input_type=settings.document_input_type,
        connector=OpenAICompatConnector(
            OpenAICompatSpec(
                api_base=settings.api_base,
                api_key=settings.api_key,
                timeout_s=settings.request_timeout_s,
            )
        ),
    )


def _embed_st(
    settings: SentenceTransformersEmbeddingSettings, registry: ResourceRegistry | None
) -> Any:
    if settings.dim is None:
        raise _missing_dim("st")
    from memspine.clients.weights.sentence_transformers import SentenceTransformerSpec, st_weights
    from memspine.services.embedding.st import SentenceTransformersEmbedding

    return SentenceTransformersEmbedding(
        settings.model,
        settings.dim,
        query_instruction=settings.query_instruction,
        query_prompt_name=settings.query_prompt_name,
        document_prompt_name=settings.document_prompt_name,
        weights=st_weights(
            SentenceTransformerSpec(
                settings.model, settings.device, settings.dtype, settings.trust_remote_code
            ),
            registry=registry,
        ),
    )


# -- llm (OpenAICompat connector) -------------------------------------------------------------------


def _openai_compat_llm(
    route_model: str,
    settings: Any,
    *,
    provider: str,
    api_base: str | None = None,
    api_key: str | None = None,
) -> Any:
    from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec
    from memspine.services.llm.openai_compat import OpenAICompatLLM

    return OpenAICompatLLM(
        route_model,
        connector=OpenAICompatConnector(
            OpenAICompatSpec(
                api_base=api_base,
                api_key=api_key,
                api_key_env=getattr(settings, "api_key_env", None),
                family=provider,
            )
        ),
        timeout_seconds=settings.timeout_seconds,
        no_think=settings.no_think,
        temperature=settings.temperature,
        top_p=settings.top_p,
        max_tokens=settings.max_tokens,
        provider=provider,
    )


def _secret(value: Any) -> str | None:
    return value.get_secret_value() if value is not None else None


def _llm_openai_compat(settings: OpenAICompatRoleSettings, _ctx: Any) -> Any:
    return _openai_compat_llm(
        settings.model,
        settings,
        provider="openai_compatible",
        api_base=settings.api_base,
        api_key=_secret(settings.api_key),
    )


def _llm_ollama(settings: OllamaRoleSettings, _ctx: Any) -> Any:
    return _openai_compat_llm(
        settings.model, settings, provider="ollama", api_base=settings.api_base
    )


def _llm_vllm(settings: VllmRoleSettings, _ctx: Any) -> Any:
    return _openai_compat_llm(
        settings.model,
        settings,
        provider="vllm",
        api_base=settings.api_base,
        api_key=_secret(settings.api_key),
    )


def _llm_lmstudio(settings: OpenAICompatRoleSettings, _ctx: Any) -> Any:
    return _openai_compat_llm(
        settings.model,
        settings,
        provider="lmstudio",
        api_base=settings.api_base or "http://localhost:1234/v1",
        api_key=_secret(settings.api_key),
    )


def _llm_llamacpp_server(settings: OpenAICompatRoleSettings, _ctx: Any) -> Any:
    return _openai_compat_llm(
        settings.model,
        settings,
        provider="llamacpp_server",
        api_base=settings.api_base or "http://localhost:8080/v1",
        api_key=_secret(settings.api_key),
    )


def _llm_bedrock(settings: BedrockRoleSettings, _ctx: Any) -> Any:
    from memspine.clients.bedrock import BedrockConnector, BedrockSpec
    from memspine.services.llm.openai_compat import OpenAICompatLLM

    service = OpenAICompatLLM(
        settings.model,
        connector=BedrockConnector(BedrockSpec(settings.aws_region, settings.timeout_seconds)),
        timeout_seconds=settings.timeout_seconds,
        no_think=settings.no_think,
        temperature=settings.temperature,
        top_p=settings.top_p,
        max_tokens=settings.max_tokens,
        provider="bedrock",
    )
    service.supports_json_schema = False
    return service


def _llm_anyllm(settings: AnyLLMRoleSettings, _ctx: Any) -> Any:
    from memspine.clients.anyllm import AnyLLMConnector
    from memspine.services.llm.openai_compat import OpenAICompatLLM

    service = OpenAICompatLLM(
        settings.model,
        connector=AnyLLMConnector(
            settings.upstream_provider, api_key_env=settings.api_key_env, api_base=settings.api_base
        ),
        timeout_seconds=settings.timeout_seconds,
        no_think=settings.no_think,
        temperature=settings.temperature,
        top_p=settings.top_p,
        max_tokens=settings.max_tokens,
        provider="anyllm",
    )
    service.supports_json_schema = settings.supports_json_schema
    return service


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
    if name == "onnx":
        from memspine.clients.weights.onnx_catalogue import model_embedder_id, resolve_model_spec

        def onnx_identity(settings: OnnxEmbeddingSettings) -> dict[str, Any]:
            spec = resolve_model_spec(settings.model, settings.model_spec)
            return {
                "embedder_id": model_embedder_id(settings.model, settings.model_spec),
                "dimension": spec.dim,
            }

        return onnx_identity
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
    if name == "openai_compatible":
        return lambda settings: {
            "embedder_id": (
                f"openai_compatible:{settings.model}"
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
            "openai_compatible",
            OpenAICompatRoleSettings,
            _llm_openai_compat,
            "llm",
            True,
            (),
            None,
        ),
        (LLM_PROVIDERS, "llm", "ollama", OllamaRoleSettings, _llm_ollama, "llm", True, (), None),
        (LLM_PROVIDERS, "llm", "vllm", VllmRoleSettings, _llm_vllm, "llm", True, (), None),
        (
            LLM_PROVIDERS,
            "llm",
            "bedrock",
            BedrockRoleSettings,
            _llm_bedrock,
            "llm",
            True,
            (DependencyRequirement("package", "boto3"),),
            "aws",
        ),
        (
            LLM_PROVIDERS,
            "llm",
            "anyllm",
            AnyLLMRoleSettings,
            _llm_anyllm,
            "llm",
            True,
            (DependencyRequirement("package", "any_llm"),),
            "anyllm",
        ),
        (
            LLM_PROVIDERS,
            "llm",
            "lmstudio",
            OpenAICompatRoleSettings,
            _llm_lmstudio,
            "llm",
            True,
            (),
            None,
        ),
        (
            LLM_PROVIDERS,
            "llm",
            "llamacpp_server",
            OpenAICompatRoleSettings,
            _llm_llamacpp_server,
            "llm",
            True,
            (),
            None,
        ),
        (
            EMBEDDERS,
            "embedding",
            "onnx",
            OnnxEmbeddingSettings,
            _embed_onnx,
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
            "openai_compatible",
            OpenAICompatEmbeddingSettings,
            _embed_openai_compat,
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
