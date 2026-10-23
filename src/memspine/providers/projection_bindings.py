"""Persisted identity checks for event-log projector targets.

Projector offsets are meaningful only for the projection target that produced
them. This module fails closed when a checkpoint would otherwise be reused by
a different provider or target. Controlled rebinds require an activated switch
manifest, a matching persisted source binding, and the verified target checkpoint.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
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
from memspine.services.storage.sqlite.schema import (
    projector_offsets,
    provider_projector_bindings,
)
from memspine.store.projections.projector import Projector

_log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ProjectionTargetBinding:
    deployment_id: str
    projector_name: str
    provider: str
    target_fingerprint: str
    generation_id: str = "active"
    source_provider: str | None = None
    source_target_fingerprint: str | None = None
    source_generation_id: str | None = None
    verified_checkpoint_seq: int | None = None


class SqlProjectionBindingStore:
    """Atomically bind a projector checkpoint to its provider target."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def bound_provider(self, deployment_id: str, projector_name: str) -> str | None:
        """The provider NAME a projector is bound to (``graph:ladybug:v1`` -> ``ladybug``),
        or None when it has no binding yet. ADR-081 pins ``auto`` selections to it."""
        async with self._engine.connect() as conn:
            provider = await conn.scalar(
                select(provider_projector_bindings.c.provider).where(
                    provider_projector_bindings.c.deployment_id == deployment_id,
                    provider_projector_bindings.c.projector_name == projector_name,
                )
            )
        if provider is None:
            return None
        parts = str(provider).split(":")
        return parts[1] if len(parts) > 1 else str(provider)

    async def adopt_renamed_provider(
        self, target: ProjectionTargetBinding, old_provider: str
    ) -> bool:
        """ADR-081: rewrite a binding recorded under a renamed provider identity (for example
        ``graph:sqlite_adjacency:v1``) to ``target`` (compare-and-swap on the old identity and
        the active generation). Only for renames whose physical target is unchanged. Returns
        True when a row was adopted."""
        async with self._engine.begin() as conn:
            result = await conn.execute(
                update(provider_projector_bindings)
                .where(
                    provider_projector_bindings.c.deployment_id == target.deployment_id,
                    provider_projector_bindings.c.projector_name == target.projector_name,
                    provider_projector_bindings.c.provider == old_provider,
                    provider_projector_bindings.c.generation_id == target.generation_id,
                )
                .values(
                    provider=target.provider,
                    target_fingerprint=target.target_fingerprint,
                    updated_at=datetime.now(UTC).isoformat(),
                )
            )
        adopted = bool(result.rowcount)
        if adopted:
            _log.info(
                "provider.binding_renamed",
                projector=target.projector_name,
                old=old_provider,
                new=target.provider,
            )
        return adopted

    async def ensure(self, target: ProjectionTargetBinding) -> str:
        """Bind a target generation, allowing only a verified compare-and-swap.

        A non-zero legacy offset without a binding is ambiguous: the target may
        have been changed while keeping the same projector name. It must be
        rebuilt or moved through a verified switch before startup can continue.
        """
        async with self._engine.begin() as conn:
            row = (
                await conn.execute(
                    select(
                        provider_projector_bindings.c.provider,
                        provider_projector_bindings.c.target_fingerprint,
                        provider_projector_bindings.c.generation_id,
                    ).where(
                        provider_projector_bindings.c.deployment_id == target.deployment_id,
                        provider_projector_bindings.c.projector_name == target.projector_name,
                    )
                )
            ).first()
            if row is not None:
                if (
                    row.provider == target.provider
                    and row.target_fingerprint == target.target_fingerprint
                    and row.generation_id == target.generation_id
                ):
                    return str(row.generation_id)
                if target.source_provider is None:
                    self._require_match(target, row.provider, row.target_fingerprint)
                    raise ConfigError(
                        f"projector {target.projector_name!r} is bound to generation "
                        f"{row.generation_id!r}, but the selected generation is "
                        f"{target.generation_id!r}; a verified projection switch is required"
                    )
                source_name = row.provider.split(":")
                source_name = source_name[1] if len(source_name) > 1 else row.provider
                source_matches = (
                    source_name == target.source_provider
                    and row.target_fingerprint == target.source_target_fingerprint
                    and row.generation_id == target.source_generation_id
                )
                if not source_matches:
                    raise ConfigError(
                        f"projector {target.projector_name!r} no longer matches the verified "
                        "switch source; refusing to reuse or replace its offset"
                    )
                offset = await conn.scalar(
                    select(projector_offsets.c.last_seq).where(
                        projector_offsets.c.projector_name == target.projector_name
                    )
                )
                current_offset = int(offset or 0)
                if current_offset != target.verified_checkpoint_seq:
                    raise ConfigError(
                        f"projector {target.projector_name!r} offset does not match the "
                        "verified target checkpoint; startup is blocked"
                    )
                result = await conn.execute(
                    update(provider_projector_bindings)
                    .where(
                        provider_projector_bindings.c.deployment_id == target.deployment_id,
                        provider_projector_bindings.c.projector_name == target.projector_name,
                        provider_projector_bindings.c.provider == row.provider,
                        provider_projector_bindings.c.target_fingerprint == row.target_fingerprint,
                        provider_projector_bindings.c.generation_id == row.generation_id,
                    )
                    .values(
                        provider=target.provider,
                        target_fingerprint=target.target_fingerprint,
                        generation_id=target.generation_id,
                        updated_at=datetime.now(UTC).isoformat(),
                    )
                )
                if result.rowcount != 1:
                    raise ConfigError("projector binding changed during verified activation")
                return target.generation_id

            offset = await conn.scalar(
                select(projector_offsets.c.last_seq).where(
                    projector_offsets.c.projector_name == target.projector_name
                )
            )
            if offset is not None and int(offset) > 0:
                raise ConfigError(
                    f"projector {target.projector_name!r} has a non-zero legacy offset "
                    "without a provider target binding; a verified projection rebuild or "
                    "switch is required before startup"
                )

            if target.source_provider is not None:
                raise ConfigError(
                    f"projector {target.projector_name!r} has no source binding for the verified "
                    "switch; refusing to create a target binding"
                )
            generation_id = target.generation_id
            values = {
                "deployment_id": target.deployment_id,
                "projector_name": target.projector_name,
                "provider": target.provider,
                "target_fingerprint": target.target_fingerprint,
                "generation_id": generation_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
            dialect = conn.dialect.name
            statement: Any
            if dialect == "sqlite":
                statement = sqlite_insert(provider_projector_bindings).values(**values)
                statement = statement.on_conflict_do_nothing(
                    index_elements=["deployment_id", "projector_name"]
                )
            elif dialect == "postgresql":
                statement = pg_insert(provider_projector_bindings).values(**values)
                statement = statement.on_conflict_do_nothing(
                    index_elements=["deployment_id", "projector_name"]
                )
            else:
                raise ConfigError("storage SQL dialect cannot persist projector target bindings")
            await conn.execute(statement)

            saved = (
                await conn.execute(
                    select(
                        provider_projector_bindings.c.provider,
                        provider_projector_bindings.c.target_fingerprint,
                        provider_projector_bindings.c.generation_id,
                    ).where(
                        provider_projector_bindings.c.deployment_id == target.deployment_id,
                        provider_projector_bindings.c.projector_name == target.projector_name,
                    )
                )
            ).one()
            self._require_match(target, saved.provider, saved.target_fingerprint)
            return str(saved.generation_id)

    @staticmethod
    def _require_match(
        target: ProjectionTargetBinding,
        saved_provider: str,
        saved_fingerprint: str,
    ) -> None:
        if saved_provider != target.provider or saved_fingerprint != target.target_fingerprint:
            raise ConfigError(
                f"projector {target.projector_name!r} is bound to provider "
                f"{saved_provider!r}, but configuration selects {target.provider!r} or a "
                "different target; startup is blocked until a verified projection switch "
                "is completed"
            )


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
    "ProjectionTargetBinding",
    "SqlProjectionBindingStore",
    "projection_target_bindings",
    "projection_target_fingerprint",
    "retire_graph_mirror_state",
]


def _effective_embedding(config: MemspineConfig) -> Any:
    # Keep the embedding profile composition identical to the container's build path.
    from memspine.runtime.container import resolved_embedding

    return resolved_embedding(config)
