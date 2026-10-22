"""Decider selection (slot ``model.decider``, RM-113): config -> descriptor -> adapter.

The engine and the read pipeline call these helpers instead of constructing decider adapters
themselves. The descriptors live in :mod:`memspine.providers.builtin.decider`; construction is
lazy (no model loads until first use), so the engine may rebuild a decider when its config
changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from memspine.providers.contexts import DeciderBuildContext
from memspine.providers.registry import DECIDERS, get_registry, validate_settings

if TYPE_CHECKING:
    from memspine.services.decider.base import DecisionProvider
    from memspine.services.decider.decider import Decider

__all__ = ["build_decider", "build_decision_provider", "decider_names"]


def decider_names() -> tuple[str, ...]:
    get_registry("deciders")
    return DECIDERS.names()


def _construct(name: str, settings: dict[str, Any], ctx: DeciderBuildContext) -> Any:
    get_registry("deciders")
    descriptor = DECIDERS.get(name)
    typed = validate_settings(descriptor, settings)
    return descriptor.builder.construct(typed, ctx)  # type: ignore[attr-defined]


def build_decider(
    kind: str,
    model: str = "manjunathshiva/opendecider-nano",
    device: str = "cpu",
    *,
    threads: int = 0,
    backend: str = "torch",
    workers: int = 1,
    dtype: str = "float32",
    registry: Any = None,
) -> Decider:
    """``read.decider`` (``heuristic`` | ``opendecider``) -> the I28 task decider (the model
    loads on first use). ``registry`` shares the weights per process; the caller releases
    the decider's lease with its ``aclose``."""
    if kind not in ("heuristic", "opendecider"):
        raise ValueError(f"decider must be 'heuristic' or 'opendecider', got {kind!r}")
    settings: dict[str, Any] = {}
    if kind == "opendecider":
        settings = {
            "model": model,
            "device": device,
            "threads": threads,
            "backend": backend,
            "workers": workers,
            "dtype": dtype,
        }
    return cast("Decider", _construct(kind, settings, DeciderBuildContext(registry=registry)))


def build_decision_provider(settings: Any, extraction: Any) -> DecisionProvider:
    """``decision.provider: gliner2`` -> the H24 decision provider over the extraction pool.
    ``settings`` is the resolved ``gliner_decision`` profile (already validated)."""
    get_registry("deciders")
    descriptor = DECIDERS.get("gliner2")
    built = descriptor.builder.construct(  # type: ignore[attr-defined]
        settings, DeciderBuildContext(extraction=extraction)
    )
    return cast("DecisionProvider", built)
