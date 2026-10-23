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

from sqlalchemy.ext.asyncio import AsyncEngine

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
from memspine.services.storage.sqlite.schema import (
    projector_offsets,
    provider_projector_bindings,
)
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
RETIRED_GRAPH_MIRROR_PROJECTOR = "graph_neo4j"
RETIRED_GRAPH_MIRROR_STAGE = "graph_projection"
RETIRED_GRAPH_MIRROR_SCOPE = ("default", "graph-neo4j")


async def retire_graph_mirror_state(storage: Any, engine: AsyncEngine) -> dict[str, int]:
    """Drop what a store that once ran the removed graph mirror still holds (idempotent).

    Left alone, these rows would never fail a start, but they are not inert: a stale
    ``graph_neo4j`` high-water mark is the minimum the rolling event-log prune respects (so
    pruning would stop for good), its binding row would show in the projection diagnostics,
    and queued ``graph_projection`` items would count towards the outbox backlog that
    backpressure reads. The rows deleted are the mirror projector's offset and binding, and
    the outbox jobs, progress and items of its private ``(default, graph-neo4j)`` scope and
    stage. Nothing else is touched: the mirror's Neo4j database itself is outside the engine
    and is left for the operator to drop. Returns the count removed per kind."""
    from sqlalchemy import delete, inspect

    from memspine.services.storage.outbox_tables import outbox, outbox_item, outbox_progress

    removed = {"offset": 0, "binding": 0, "outbox_jobs": 0, "outbox_progress": 0, "outbox_items": 0}
    if await storage.get_offset(RETIRED_GRAPH_MIRROR_PROJECTOR):
        await storage.reset_offset(RETIRED_GRAPH_MIRROR_PROJECTOR)
        removed["offset"] = 1
    tenant, owner = RETIRED_GRAPH_MIRROR_SCOPE
    async with engine.begin() as conn:
        tables = set(await conn.run_sync(lambda sync: set(inspect(sync).get_table_names())))
        if provider_projector_bindings.name in tables:
            result = await conn.execute(
                delete(provider_projector_bindings).where(  # every deployment lost the mirror
                    provider_projector_bindings.c.projector_name == RETIRED_GRAPH_MIRROR_PROJECTOR
                )
            )
            removed["binding"] = int(result.rowcount or 0)
        if projector_offsets.name in tables:  # a SQL offset the records port does not own
            result = await conn.execute(
                delete(projector_offsets).where(
                    projector_offsets.c.projector_name == RETIRED_GRAPH_MIRROR_PROJECTOR
                )
            )
            removed["offset"] = max(removed["offset"], int(result.rowcount or 0))
        for key, table, stage_column in (
            ("outbox_jobs", outbox, outbox.c.stage),
            ("outbox_progress", outbox_progress, outbox_progress.c.job_kind),
            ("outbox_items", outbox_item, outbox_item.c.stage),
        ):
            if table.name not in tables:
                continue
            result = await conn.execute(
                delete(table).where(
                    table.c.tenant == tenant,
                    table.c.owner == owner,
                    stage_column == RETIRED_GRAPH_MIRROR_STAGE,
                )
            )
            removed[key] = int(result.rowcount or 0)
    if any(removed.values()):
        _log.info("provider.graph_mirror_retired", **removed)
    return removed


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
    "RETIRED_GRAPH_MIRROR_PROJECTOR",
    "RETIRED_GRAPH_MIRROR_SCOPE",
    "RETIRED_GRAPH_MIRROR_STAGE",
    "projection_target_bindings",
    "projection_target_fingerprint",
    "retire_graph_mirror_state",
]


def _effective_embedding(config: MemspineConfig) -> Any:
    # Keep the embedding profile composition identical to the container's build path.
    from memspine.runtime.container import resolved_embedding

    return resolved_embedding(config)
