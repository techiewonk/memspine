"""Built-in rerank descriptors (slot ``model.rerank``): settings -> weights connector -> adapter.

The one rerank factory (RM-113 collapsed the second ``rerank/factory.py`` into this module and
the selection helpers into :mod:`memspine.providers.rerank`). Each builder imports its adapter
and connector lazily, so importing this module loads no model. An Engine build passes the
process weight registry (ADR-079 / RM-73) and registers this Engine's lease release with its
lifecycle; a standalone build (no Engine) loads privately.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from memspine.config.schema.providers import (
    OnnxRerankerSettings,
    FlashrankRerankerSettings,
    JinaRerankerSettings,
    OpenAICompatRerankerSettings,
    Qwen3RerankerSettings,
)
from memspine.providers.contexts import SharedBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.registry import RERANKERS, ProviderDescriptor
from memspine.providers.shared import ResourceRegistry
from memspine.services.base import CapabilityManifest

__all__ = ["register"]


def _onnx(settings: OnnxRerankerSettings, registry: ResourceRegistry | None) -> Any:
    from memspine.clients.weights.onnx import rerank_weights
    from memspine.services.rerank.onnx import OnnxReranker

    return OnnxReranker(
        settings.model, batch_size=settings.batch_size, model_spec=settings.model_spec,
        weights=rerank_weights(
            settings.model, model_spec=settings.model_spec, threads=settings.threads,
            providers=settings.providers, local_files_only=settings.local_files_only,
            registry=registry,
        ),
    )


def _flashrank(settings: FlashrankRerankerSettings, registry: ResourceRegistry | None) -> Any:
    from memspine.clients.weights.flashrank import ranker_weights
    from memspine.services.rerank.flashrank import FlashrankReranker

    return FlashrankReranker(
        settings.model, weights=ranker_weights(settings.model, registry=registry)
    )


def _openai_compat(
    settings: OpenAICompatRerankerSettings, _registry: ResourceRegistry | None
) -> Any:
    from memspine.clients.openai_compat import OpenAICompatConnector, OpenAICompatSpec
    from memspine.services.rerank.openai_compat import OpenAICompatReranker

    return OpenAICompatReranker(
        settings.model,
        request_timeout_s=settings.request_timeout_s,
        connector=OpenAICompatConnector(
            OpenAICompatSpec(
                api_base=settings.api_base,
                api_key=settings.api_key,
                api_key_env=settings.api_key_env,
                timeout_s=settings.request_timeout_s,
            )
        ),
    )


def _qwen3(settings: Qwen3RerankerSettings, registry: ResourceRegistry | None) -> Any:
    from memspine.services.rerank.qwen3 import Qwen3Reranker

    weights = None
    if registry is not None:
        from memspine.clients.weights.transformers import (
            Qwen3RerankerSpec,
            qwen3_reranker_weights,
        )

        weights = qwen3_reranker_weights(
            Qwen3RerankerSpec(settings.model, settings.device, settings.quant), registry=registry
        )
    return Qwen3Reranker(
        settings.model,
        instruction=settings.instruction,
        device=settings.device,
        quant=settings.quant,
        batch_size=settings.batch_size,
        max_length=settings.max_length,
        weights=weights,
    )


def _jina(settings: JinaRerankerSettings, registry: ResourceRegistry | None) -> Any:
    from memspine.services.rerank.jina import JinaReranker

    weights = None
    if registry is not None:
        from memspine.clients.weights.transformers import JinaRerankerSpec, jina_reranker_weights

        weights = jina_reranker_weights(
            JinaRerankerSpec(settings.model, settings.device), registry=registry
        )
    return JinaReranker(
        settings.model,
        device=settings.device,
        max_documents=settings.max_documents,
        weights=weights,
    )


@dataclass(frozen=True, slots=True)
class _RerankerBuilder:
    ref: ProviderRef
    factory: Callable[[Any, ResourceRegistry | None], Any]
    capabilities: frozenset[str] = frozenset({"rerank"})

    def construct(self, settings: Any, registry: ResourceRegistry | None = None) -> Any:
        return self.factory(settings, registry)

    async def build(self, settings: Any, ctx: Any) -> BuildResult[Any]:
        shared = getattr(ctx, "shared", None)
        if not isinstance(shared, SharedBuildContext):
            # Standalone build (no Engine lifecycle): private weights, freed with the object.
            return BuildResult(self.construct(settings), self.ref, (), self.capabilities)
        # Engine build: weights come from the process registry; this Engine's lease is
        # released by its lifecycle, never by another Engine (ADR-079 / RM-73).
        service = self.construct(settings, shared.resource_registry())
        release = getattr(service, "aclose", None)
        refs = (
            (shared.resources.own(f"reranker.weights:{self.ref.name}", release),)
            if callable(release)
            else ()
        )
        return BuildResult(service, self.ref, refs, self.capabilities)


_RERANKERS: tuple[
    tuple[
        str,
        type[BaseModel],
        Callable[[Any, ResourceRegistry | None], Any],
        tuple[DependencyRequirement, ...],
        str | None,
    ],
    ...,
] = (
    ("onnx", OnnxRerankerSettings, _onnx, (), None),
    (
        "flashrank",
        FlashrankRerankerSettings,
        _flashrank,
        (DependencyRequirement("package", "flashrank"),),
        "rerank",
    ),
    ("openai_compatible", OpenAICompatRerankerSettings, _openai_compat, (), None),
    (
        "qwen3",
        Qwen3RerankerSettings,
        _qwen3,
        (
            DependencyRequirement("package", "transformers"),
            DependencyRequirement("package", "torch"),
        ),
        "st",
    ),
    (
        "jina",
        JinaRerankerSettings,
        _jina,
        (
            DependencyRequirement("package", "transformers"),
            DependencyRequirement("package", "torch"),
        ),
        "st",
    ),
)


def register() -> None:
    """Register lazy rerank descriptors; importing this module loads no adapter."""
    for name, settings_model, factory, requirements, extra in _RERANKERS:
        if name in RERANKERS:
            continue
        ref = ProviderRef("rerank", name)
        RERANKERS.register(
            name,
            ProviderDescriptor(
                ref=ref,
                settings_model=settings_model,
                declared_capabilities=frozenset({"rerank"}),
                requirements=requirements,
                builder=_RerankerBuilder(ref, factory),
                service_manifest=CapabilityManifest(
                    name, "rerank", provides=("rerank",), extra=extra
                ),
                requires_deployment_id=name == "openai_compatible",
            ),
        )
