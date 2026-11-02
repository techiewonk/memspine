"""RM-155: validate the selected providers when the config is loaded (D-10: fail fast).

The config schema keeps every provider's ``settings`` as provider-owned JSON (ADR-081 /
ADR-082), so a key the provider does not take, an unknown provider name or a missing
``deployment.id`` for a shared provider used to pass ``load_config`` and fail only when the
Engine built that provider. :func:`validate_providers` runs the build path's checks on the
loaded config without building anything:

- every built slot names a registered provider (``storage.records`` / ``vector`` / ``cache``
  always, ``storage.lexical`` with ``read.hybrid``, ``storage.graph`` with associative memory,
  ``storage.documents`` when set; ``model.embedding`` with its preset applied;
  ``model.llm.<role>`` and its escalation targets; ``model.rerank.chain[i]``);
- its ``settings`` validate against the descriptor's ``settings_model`` (an LLM slot's typed
  ``timeout_s`` included, as the build passes it);
- a descriptor with ``requires_deployment_id`` has an explicit ``deployment.id``;
- the ADR-081 ``deployment.locality`` / ``data_dir`` rules
  (:func:`memspine.providers.locality.validate_deployment_locality`).

Every problem is reported in one :class:`ProviderValidationError` naming the key path and the
provider. Pure: it reads descriptors (``get_registry`` registers the built-in descriptors,
whose modules import no optional driver) and never opens a connection, imports a driver or
touches a file. ``model.extraction`` / ``model.decider`` settings are validated by the schema
itself (strict ``gliner2`` / ``opendecider`` models).

A provider name a pinned plug-in (``plugins[].slot`` = ``<kind>:<name>``) will register is
not known until the Engine enables its plug-ins, so it is left to the build.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from memspine.exceptions import ConfigError

if TYPE_CHECKING:
    from pydantic import BaseModel

    from memspine.config.schema import MemspineConfig
    from memspine.providers.registry import ProviderDescriptor

__all__ = [
    "ProviderValidationError",
    "effective_embedding_slot",
    "llm_slot_settings",
    "validate_providers",
]


class ProviderValidationError(ConfigError):
    """One or more selected providers have unknown names, invalid settings or a missing
    ``deployment.id``; the message lists each with its key path and provider."""


def llm_slot_settings(descriptor: ProviderDescriptor, slot: Any) -> BaseModel:
    """ADR-082: a ``model.llm.<role>`` slot's native settings plus its typed ``timeout_s``
    (the adapters' ``timeout_seconds``), validated by the selected descriptor."""
    from memspine.providers.registry import validate_settings

    values: dict[str, Any] = dict(slot.settings)
    if "timeout_seconds" in descriptor.settings_model.model_fields:
        values["timeout_seconds"] = slot.timeout_s
    return validate_settings(descriptor, values)


def effective_embedding_slot(config: MemspineConfig) -> Any:
    """``model.embedding`` with ``model.embedding.preset`` applied (MD-02), without the
    ``embed.profile`` tap event the build emits."""
    cfg = config.model.embedding
    if cfg.preset is None:
        return cfg
    from memspine.models.embedding_profiles import resolve_profile

    configured_device = cfg.settings.get("device")
    profile = resolve_profile(
        cfg.preset,
        device=configured_device if isinstance(configured_device, str) else None,
    )
    return profile.apply(cfg)


def _plugin_names(config: MemspineConfig) -> set[tuple[str, str]]:
    names: set[tuple[str, str]] = set()
    for pin in config.plugins:
        kind, sep, name = str(getattr(pin, "slot", "")).partition(":")
        if sep and name:
            names.add((kind, name))
    return names


def validate_providers(config: MemspineConfig) -> None:
    """Apply the provider and deployment checks of the module docstring; raise
    :class:`ProviderValidationError` (or the locality module's
    :class:`~memspine.providers.locality.DeploymentLocalityError`) listing every problem."""
    from memspine.core.registry import dependency_closure
    from memspine.providers.registry import (
        ProviderDescriptor,
        ProviderRegistryError,
        get_registry,
        validate_settings,
    )

    explicit_id = config.deployment.id is not None
    plugins = _plugin_names(config)
    errors: list[str] = []

    def descriptor(kind: str, name: str, path: str) -> ProviderDescriptor | None:
        registry = get_registry(kind)
        found = registry.find(name)
        if isinstance(found, ProviderDescriptor):
            return found
        if (kind, name) not in plugins:
            errors.append(
                f"{path}.provider: unknown provider {name!r} (valid: {', '.join(registry.names())})"
            )
        return None

    def check(
        path: str, kind: str, name: str, settings: Mapping[str, Any], llm_slot: Any = None
    ) -> None:
        found = descriptor(kind, name, path)
        if found is None:
            return
        provider = f"{found.ref.category}.{found.ref.name}"
        if found.requires_deployment_id and not explicit_id:
            errors.append(
                f"{path} (provider {name!r}): deployment.id is required by shared provider "
                f"{provider}; set deployment.id (env MEMSPINE_DEPLOYMENT__ID)"
            )
        try:
            if llm_slot is not None:
                llm_slot_settings(found, llm_slot)
            else:
                validate_settings(found, settings)
        except ProviderRegistryError as exc:
            errors.append(f"{path}.settings (provider {name!r}): {exc}")

    enabled, _ = dependency_closure(config.enabled_memories())
    storage = config.storage
    slots: list[tuple[str, str, Any]] = [
        ("records", "storage_backends", storage.records),
        ("vector", "vector_stores", storage.vector),
        ("cache", "cache_backends", storage.cache),
    ]
    if config.read.hybrid:
        slots.append(("lexical", "lexical_indexes", storage.lexical))
    if "associative" in enabled:
        slots.append(("graph", "graph_stores", storage.graph))
    if storage.documents is not None:
        slots.append(("documents", "document_stores", storage.documents))
    for slot_name, kind, slot in slots:
        if slot.provider == "auto":
            continue  # resolved (and pinned) at build; the locality rules cover it
        check(f"storage.{slot_name}", kind, slot.provider, slot.settings)

    try:
        embedding = effective_embedding_slot(config)
    except ConfigError as exc:
        errors.append(f"model.embedding.preset: {exc}")
    else:
        check("model.embedding", "embedders", embedding.provider, embedding.settings)

    for role, llm in config.model.llm.items():
        check(f"model.llm.{role}", "llm_providers", llm.provider, llm.settings, llm)
        if llm.escalation is not None:
            for index, target in enumerate(llm.escalation.targets):
                path = f"model.llm.{role}.escalation.targets[{index}]"
                check(path, "llm_providers", target.provider, target.settings, target)

    from memspine.providers.rerank import validate_reranker_settings

    for index, stage in enumerate(config.model.rerank.chain):
        path = f"model.rerank.chain[{index}]"
        if stage.provider == "off":
            errors.append(
                f"{path}: 'off' is not a reranker provider; use model.rerank.chain: [] to turn "
                "reranking off"
            )
            continue
        if ("rerankers", stage.provider) in plugins:
            continue
        try:
            validate_reranker_settings(
                stage.provider, stage.settings, explicit_deployment_id=explicit_id, path=path
            )
        except ConfigError as exc:
            errors.append(f"{path} (provider {stage.provider!r}): {exc}")

    if errors:
        raise ProviderValidationError("invalid provider configuration: " + "; ".join(errors))

    from memspine.providers.locality import validate_deployment_locality

    validate_deployment_locality(config, enabled=enabled)
