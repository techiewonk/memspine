"""Built-in extraction descriptor (slot ``model.extraction``, RM-113): the GLiNER2 pool.

One Engine builds one extraction pool through this descriptor (it used to be constructed in
the container). The pool leases weights from the process registry through the
``clients.weights.gliner2`` loader, so every consumer and every Engine bound to the same
checkpoint shares one load (ADR-079); the Engine lifecycle closes this Engine's pool.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import Field

from memspine.providers.contexts import ExtractionBuildContext, SharedBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import EXTRACTORS, ProviderDescriptor
from memspine.providers.settings import StrictSettings
from memspine.services.base import CapabilityManifest

__all__ = ["GlinerPoolSettings", "register"]

#: The Engine lifecycle id of the extraction pool.
POOL_RESOURCE_ID = "gliner-model-pool"


class GlinerPoolSettings(StrictSettings):
    """Pool limits; per-consumer model settings come from each ``model.extraction.<consumer>``
    / ``model.decider.route`` slot (ADR-082)."""

    #: Upper bound on simultaneously loaded GLiNER checkpoints
    #: (``model.placement.max_loaded_models``).
    max_models: int = Field(default=1, ge=1)


@dataclass(frozen=True, slots=True)
class _GlinerPoolBuilder:
    ref: ProviderRef

    async def build(self, settings: GlinerPoolSettings, ctx: Any) -> BuildResult[Any]:
        from memspine.services.extraction.gliner2 import GLiNERModelPool

        shared = ctx.shared if isinstance(ctx, ExtractionBuildContext) else ctx
        if not isinstance(shared, SharedBuildContext):
            pool = GLiNERModelPool(max_models=settings.max_models)
            return BuildResult(pool, self.ref, (), frozenset({"extraction"}))
        pool = GLiNERModelPool(max_models=settings.max_models, registry=shared.resource_registry())
        resource = shared.resources.own(POOL_RESOURCE_ID, pool.aclose)
        return BuildResult(pool, self.ref, (resource,), frozenset({"extraction"}))


def register() -> None:
    """Register the lazy GLiNER2 extraction descriptor (no GLiNER import here)."""
    if "gliner2" in EXTRACTORS:
        return
    ref = ProviderRef("extraction", "gliner2")
    EXTRACTORS.register(
        ref.name,
        ProviderDescriptor(
            ref=ref,
            settings_model=GlinerPoolSettings,
            declared_capabilities=frozenset({"extraction"}),
            # The pool itself needs no package: consumers check gliner2 when they bind.
            requirements=(),
            builder=_GlinerPoolBuilder(ref),
            service_manifest=CapabilityManifest(
                "gliner2", "extraction", provides=("extraction",), extra="ner"
            ),
        ),
    )
