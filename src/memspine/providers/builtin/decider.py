"""Built-in decider descriptors (slot ``model.decider``, RM-113).

A decider chooses among described options without generating. Two ports live in this slot:

* ``gliner2``: the H24 :class:`~memspine.services.decider.base.DecisionProvider` (calibrated
  choice among described options, ``model.decider.route``). It leases the Engine's extraction
  pool, so it shares the GLiNER weights connector with the extraction consumers.
* ``heuristic`` and ``opendecider``: the I28 task-level
  :class:`~memspine.services.decider.decider.Decider` (``model.decider.read``).

Builders expose ``construct`` (sync, lazy: no model loads until first use) for the engine's
config-reloadable decider, and ``build`` for the async descriptor contract. Reserved future
names (JEPA-style, YOLO-style deciders) are not registered.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pydantic import Field

from memspine.config.schema.model import GlinerSettings
from memspine.providers.contexts import DeciderBuildContext, SharedBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.registry import DECIDERS, ProviderDescriptor
from memspine.providers.settings import EmptySettings, StrictSettings
from memspine.services.base import CapabilityManifest

__all__ = ["OpenDeciderSettings", "register"]


class OpenDeciderSettings(StrictSettings):
    """``model.decider.read`` settings (plus its typed ``workers``) for OpenDecider-nano."""

    model: str = "manjunathshiva/opendecider-nano"
    device: Literal["cpu", "cuda", "mps", "auto"] = "cpu"
    threads: int = Field(default=0, ge=0)
    backend: Literal["torch", "onnx"] = "torch"
    workers: int = Field(default=1, ge=1)
    dtype: str = "float32"


def _shared(ctx: Any) -> SharedBuildContext | None:
    shared = ctx.shared if isinstance(ctx, DeciderBuildContext) else ctx
    return shared if isinstance(shared, SharedBuildContext) else None


@dataclass(frozen=True, slots=True)
class _DeciderBuilder:
    ref: ProviderRef
    capabilities: frozenset[str]

    def construct(self, settings: Any, ctx: Any = None) -> Any:
        name = self.ref.name
        if name == "heuristic":
            from memspine.services.decider.decider import HeuristicDecider

            return HeuristicDecider()
        if name == "opendecider":
            from memspine.services.decider.decider import OpenDeciderDecider

            shared = _shared(ctx)
            return OpenDeciderDecider(
                settings.model,
                settings.device,
                threads=settings.threads,
                backend=settings.backend,
                workers=settings.workers,
                dtype=settings.dtype,
                # RM-73: weights shared per process; the holder's aclose releases its lease.
                registry=(
                    shared.resource_registry()
                    if shared is not None
                    else getattr(ctx, "registry", None)
                ),
            )
        if name == "gliner2":
            from memspine.exceptions import ConfigError
            from memspine.services.decider.gliner2 import GLiNER2Decision

            pool = ctx.extraction if isinstance(ctx, DeciderBuildContext) else None
            if pool is None:
                raise ConfigError("decision GLiNER provider needs the engine GLiNER pool")
            return GLiNER2Decision(settings, pool)
        raise ValueError(f"no decider implementation for builtin {name!r}")

    async def build(self, settings: Any, ctx: Any) -> BuildResult[Any]:
        service = self.construct(settings, ctx)
        shared = _shared(ctx)
        release = getattr(service, "aclose", None)
        refs = (
            (shared.resources.own(f"decider.weights:{self.ref.name}", release),)
            if shared is not None and callable(release)
            else ()
        )
        return BuildResult(service, self.ref, refs, self.capabilities)


def register() -> None:
    """Register the lazy decider descriptors; importing this module loads no model."""
    entries: tuple[
        tuple[str, type[Any], str, tuple[DependencyRequirement, ...], str | None], ...
    ] = (
        ("heuristic", EmptySettings, "decide", (), None),
        (
            "opendecider",
            OpenDeciderSettings,
            "decide",
            (
                DependencyRequirement("package", "torch"),
                DependencyRequirement("package", "transformers"),
            ),
            "decider",
        ),
        (
            "gliner2",
            GlinerSettings,
            "choose",
            (DependencyRequirement("package", "gliner2"),),
            "ner",
        ),
    )
    for name, settings_model, capability, requirements, extra in entries:
        if name in DECIDERS:
            continue
        ref = ProviderRef("decider", name)
        capabilities = frozenset({"decider", capability})
        DECIDERS.register(
            name,
            ProviderDescriptor(
                ref=ref,
                settings_model=settings_model,
                declared_capabilities=capabilities,
                requirements=requirements,
                builder=_DeciderBuilder(ref, capabilities),
                service_manifest=CapabilityManifest(
                    name, "decider", provides=tuple(sorted(capabilities)), extra=extra
                ),
            ),
        )
