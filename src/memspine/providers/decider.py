"""Decider selection (slot ``model.decider``, RM-113): config -> descriptor -> adapter.

The engine and the read pipeline call these helpers instead of constructing decider adapters
themselves. The descriptors live in :mod:`memspine.providers.builtin.decider`; construction is
lazy (no model loads until first use), so the engine may rebuild a decider when its config
changes.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from memspine.exceptions import ConfigError
from memspine.providers.contexts import DeciderBuildContext
from memspine.providers.registry import DECIDERS, get_registry, validate_settings

if TYPE_CHECKING:
    from memspine.config.schema.model import DeciderReadSlot
    from memspine.config.schema.root import MemspineConfig
    from memspine.services.decider.base import DecisionProvider
    from memspine.services.decider.decider import Decider

__all__ = [
    "READ_DECIDER_PROVIDERS",
    "ROUTE_DECIDER_PROVIDERS",
    "build_decider",
    "build_decision_provider",
    "build_read_decider",
    "decider_names",
    "read_decider_min_confidence",
    "read_decider_slot",
]

#: ADR-082 item 8: the providers each decider consumer implements today. Later deciders
#: (JEPA-style, YOLO-style) register as providers and join these sets.
ROUTE_DECIDER_PROVIDERS: frozenset[str] = frozenset({"gliner2"})
READ_DECIDER_PROVIDERS: frozenset[str] = frozenset({"heuristic", "opendecider"})

#: ``model.decider.read.min_confidence`` default (used when no read slot is configured, e.g.
#: a decider injected with ``Engine.set_decider``).
_DEFAULT_MIN_CONFIDENCE = 0.5


def decider_names() -> tuple[str, ...]:
    get_registry("deciders")
    return DECIDERS.names()


def _construct(name: str, settings: dict[str, Any], ctx: DeciderBuildContext) -> Any:
    get_registry("deciders")
    descriptor = DECIDERS.get(name)
    typed = validate_settings(descriptor, settings)
    return descriptor.builder.construct(typed, ctx)  # type: ignore[attr-defined]


def read_decider_slot(config: MemspineConfig) -> DeciderReadSlot | None:
    """``model.decider.read`` when it selects a model decider; None for an absent slot or a
    ``heuristic`` one (the read path keeps its heuristic rules, I28's default)."""
    slot = config.model.decider.read
    if slot is None or slot.provider == "heuristic":
        return None
    return slot


def read_decider_min_confidence(config: MemspineConfig) -> float:
    """A read-path decision below this keeps the heuristic answer (I28)."""
    slot = config.model.decider.read
    return _DEFAULT_MIN_CONFIDENCE if slot is None else slot.min_confidence


def build_read_decider(slot: DeciderReadSlot | None, *, registry: Any = None) -> Decider:
    """``model.decider.read`` -> the I28 task decider (lazy; the model loads on first use).
    None or ``heuristic`` is the heuristic decider. OpenDecider load options come from the
    slot's ``settings``; ``workers`` is the slot's typed key."""
    if slot is None or slot.provider == "heuristic":
        return build_decider("heuristic", registry=registry)
    if slot.provider != "opendecider":
        raise ConfigError(
            f"model.decider.read provider {slot.provider!r} is not implemented for the read "
            f"path (valid: {', '.join(sorted(READ_DECIDER_PROVIDERS))})"
        )
    from memspine.config.schema.model import OpenDeciderSettings

    native = OpenDeciderSettings.model_validate(dict(slot.settings))
    return build_decider(
        "opendecider",
        native.model,
        native.device,
        threads=native.threads,
        backend=native.backend,
        workers=slot.workers,
        dtype=native.dtype,
        registry=registry,
    )


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
    """``heuristic`` | ``opendecider`` -> the I28 task decider (the model
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
    """``model.decider.route`` (``gliner2``) -> the H24 decision provider over the extraction
    pool. ``settings`` is the slot's strict GLiNER settings (``gliner_settings(config,
    "route")``)."""
    get_registry("deciders")
    descriptor = DECIDERS.get("gliner2")
    built = descriptor.builder.construct(  # type: ignore[attr-defined]
        settings, DeciderBuildContext(extraction=extraction)
    )
    return cast("DecisionProvider", built)
