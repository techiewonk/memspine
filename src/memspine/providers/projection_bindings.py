"""Persisted identity checks for event-log projector targets.

Projector offsets are meaningful only for the projection target that produced
them. This module builds the target identities; the records store persists them
(``RecordsStore.projection_bindings()``, RM-112) and fails closed when a checkpoint
would otherwise be reused by a different provider or target. Controlled rebinds
require an activated switch manifest, a matching persisted source binding, and
the verified target checkpoint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from memspine.config.schema import MemspineConfig
from memspine.exceptions import ConfigError
from memspine.observability.logging import get_logger
from memspine.providers.contracts import BuildResult
from memspine.providers.engine_changes import resolved_config_fingerprint
from memspine.providers.registry import (
    get_registry,
    provider_projection_settings,
    provider_target_identity,
    validate_settings,
)
from memspine.services.embedding.base import EmbeddingService
from memspine.services.storage.records_port import ProjectionTargetBinding
from memspine.store.projections.projector import Projector

_log = get_logger(__name__)


def projection_target_bindings(
    config: MemspineConfig,
    provider_results: Mapping[str, BuildResult[Any]],
    projectors: Sequence[Projector],
    deployment_id: str,
    embedder: EmbeddingService | None,
    routes: Mapping[str, Any] | None = None,
) -> tuple[ProjectionTargetBinding, ...]:
    """Build safe, deterministic binding identities for active projections."""
    result_keys = {
        "vectors": ("vector", "vector", config.storage.vector.settings),
        "graph": ("graph.primary", "graph", config.storage.graph.settings),
    }
    lexical_settings = config.storage.lexical.settings
    if any(str(getattr(projector, "name", "")).startswith("lexical") for projector in projectors):
        for projector in projectors:
            name = str(getattr(projector, "name", ""))
            if name.startswith("lexical"):
                result_keys[name] = ("lexical", "lexical", lexical_settings)

    bindings: list[ProjectionTargetBinding] = []
    for projector in projectors:
        projector_name = str(getattr(projector, "name", ""))
        entry = result_keys.get(projector_name)
        if entry is None:
            continue
        result_key, category, raw_settings = entry
        result = provider_results.get(result_key)
        if not isinstance(result, BuildResult):
            raise ConfigError(
                f"projector {projector_name!r} has no selected provider result for binding"
            )
        provider_name = result.provider.name
        registry_kind = {
            "graph": "graph_stores",
            "vector": "vector_stores",
            "lexical": "lexical_indexes",
        }[category]
        descriptor = get_registry(registry_kind).get(provider_name)
        settings = validate_settings(descriptor, raw_settings)
        fingerprint = projection_target_fingerprint(
            config,
            category=category,
            provider=result.provider,
            settings=settings,
            deployment_id=deployment_id,
            embedder=embedder,
        )
        route = (routes or {}).get(projector_name)
        provider_identity = (
            f"{result.provider.category}:{result.provider.name}:v{result.provider.contract_version}"
        )
        if route is not None:
            if route.provider != provider_name:
                raise ConfigError(
                    f"projector {projector_name!r} selected provider does not match its verified switch target"
                )
            if route.target_fingerprint != fingerprint:
                raise ConfigError(
                    f"projector {projector_name!r} settings do not match the verified switch target fingerprint"
                )
        bindings.append(
            ProjectionTargetBinding(
                deployment_id=deployment_id,
                projector_name=projector_name,
                provider=provider_identity,
                target_fingerprint=fingerprint,
                generation_id="active" if route is None else route.generation_id,
                source_provider=None if route is None else route.source_provider,
                source_target_fingerprint=(
                    None if route is None else route.source_target_fingerprint
                ),
                source_generation_id=None if route is None else route.source_generation_id,
                verified_checkpoint_seq=(None if route is None else route.target_checkpoint_seq),
            )
        )
    return tuple(bindings)


#: ADR-081 amendment 2026-10-23: durable names the removed graph mirror (the Neo4j outbox
#: projection, ``storage.graph_mirror``) left behind. Nothing reads them any more.


def projection_target_fingerprint(
    config: MemspineConfig,
    *,
    category: str,
    provider: Any,
    settings: Any,
    deployment_id: str,
    embedder: EmbeddingService | None = None,
) -> str:
    """Fingerprint one resolved provider target before opening its native client.

    Generation openers and Engine startup use this same identity so an active
    manifest cannot authorize a provider config that merely shares its name.
    """
    from memspine.providers.placement import storage_placement

    registry_kind = {
        "graph": "graph_stores",
        "vector": "vector_stores",
        "lexical": "lexical_indexes",
    }.get(category)
    if registry_kind is None:
        raise ConfigError(f"unsupported projection category {category!r}")
    descriptor = get_registry(registry_kind).get(provider.name)
    placement = storage_placement(config, deployment_id)
    projection_settings = provider_projection_settings(descriptor, settings)
    identity: dict[str, object] = {
        "provider": {
            "category": provider.category,
            "name": provider.name,
            "contract_version": provider.contract_version,
        },
        "settings": settings if projection_settings is None else projection_settings,
        "placement": {
            "base_path": None if placement.base_path is None else str(Path(placement.base_path)),
            "in_memory": placement.in_memory,
            "owner_id": placement.owner_id,
        },
    }
    if category == "vector":
        storage_descriptor = get_registry("storage_backends").get(config.storage.records.provider)
        storage_settings = validate_settings(storage_descriptor, config.storage.records.settings)
        storage_identity = provider_target_identity(storage_descriptor, storage_settings)
        if storage_identity is not None:
            identity["sql_target"] = storage_identity
    if category == "vector" and not config.model.embedding.versioned_spaces:
        effective = _effective_embedding(config)
        embedding_descriptor = get_registry("embedders").get(effective.provider)
        embedding_settings = validate_settings(embedding_descriptor, effective.settings)
        embedding_identity = provider_target_identity(embedding_descriptor, embedding_settings)
        if (
            embedding_identity is None
            or not isinstance(embedding_identity.get("embedder_id"), str)
            or not isinstance(embedding_identity.get("dimension"), int)
        ):
            raise ConfigError(
                "selected embedding provider must expose a no-load table identity for projection preflight"
            )
        expected_table = {
            "embedder_id": embedding_identity["embedder_id"],
            "dimension": embedding_identity["dimension"],
        }
        if embedder is not None and (
            getattr(embedder, "embedder_id", None) != expected_table["embedder_id"]
            or getattr(embedder, "dim", None) != expected_table["dimension"]
        ):
            raise ConfigError(
                "runtime embedding table identity does not match the configured provider target"
            )
        identity["embedding_table"] = expected_table
    target_identity = provider_target_identity(descriptor, settings)
    if target_identity is not None:
        identity["resolved_target"] = target_identity
    return resolved_config_fingerprint(identity)


__all__ = [
    "projection_target_bindings",
    "projection_target_fingerprint",
]


def _effective_embedding(config: MemspineConfig) -> Any:
    # Keep the embedding profile composition identical to the container's build path.
    from memspine.runtime.container import resolved_embedding

    return resolved_embedding(config)
