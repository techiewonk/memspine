"""Built-in pgvector descriptor: settings to a projection binding over the lent Postgres
connector. The adapter owns the projection DDL (RM-113 item 11)."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, JsonValue

from memspine.config.schema.providers import PgvectorSettings
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.providers.contexts import VectorBuildContext
from memspine.providers.contracts import (
    BuildResult,
    DependencyRequirement,
    ProviderRef,
)
from memspine.providers.registry import VECTOR_STORES, ProviderDescriptor
from memspine.services.base import CapabilityManifest
from memspine.services.vector.base import VectorStore


def _table_name(ctx: VectorBuildContext) -> str:
    identity = "\0".join(
        (
            ctx.shared.owner_id,
            ctx.generation_id,
            ctx.embedding.embedder_id,
            str(ctx.embedding.dim),
            json.dumps(dict(ctx.profile_meta or {}), sort_keys=True, separators=(",", ":")),
        )
    )
    return "vectors_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def _projection_settings(settings: BaseModel) -> dict[str, JsonValue]:
    if not isinstance(settings, PgvectorSettings):
        raise ConfigError("pgvector projection settings received incompatible settings")
    return {
        "index": settings.index,
        "hnsw_m": settings.hnsw_m,
        "hnsw_ef_construction": settings.hnsw_ef_construction,
        "hnsw_ef_search": settings.hnsw_ef_search,
        "namespace_index": settings.namespace_index,
    }


class PgvectorBuilder:
    async def build(
        self, settings: PgvectorSettings, ctx: VectorBuildContext
    ) -> BuildResult[VectorStore]:
        if ctx.sql is None or "sql.postgres" not in ctx.sql.capabilities:
            raise ConfigError(
                "storage.vector.provider=pgvector requires storage.records.provider=postgres and its SQL access"
            )
        if ctx.versioned_spaces:
            raise ConfigError(
                "pgvector does not support model.embedding.versioned_spaces; select a vector "
                "provider "
                "that implements versioned embedding-space routing"
            )
        if ctx.quantization is not None or ctx.matryoshka_dim is not None:
            raise ConfigError(
                "pgvector does not support configured vector quantization or Matryoshka rescoring"
            )
        if settings.index == "hnsw" and ctx.embedding.dim > 2000:
            raise ConfigError(
                "pgvector HNSW supports vector dimensions through 2000; select settings.index=none "
                "for a higher-dimensional exact scan"
            )
        if settings.create_extension and not settings.provision_projection:
            raise ConfigError(
                "storage.vector.settings.create_extension requires explicit "
                "storage.vector.settings.provision_projection=true"
            )
        try:
            import pgvector.sqlalchemy  # noqa: F401  # the adapter's column type
        except ImportError:
            raise MissingServiceError("vector:pgvector", "postgres") from None
        # The records slot's opened Postgres connector (one pool per database, ADR-079): the
        # adapter borrows it and never closes it.
        connector = getattr(ctx.sql, "connector", None)
        if connector is None:
            raise ConfigError("pgvector requires the records slot's Postgres connector")

        from memspine.services.vector.pgvector_store import PgVectorProjection, PgVectorStore

        sql_ref = ctx.shared.resources.borrow("sql")
        service = PgVectorStore(
            connector,
            PgVectorProjection(
                table_name=_table_name(ctx),
                dimension=ctx.embedding.dim,
                index=settings.index,
                hnsw_m=settings.hnsw_m,
                hnsw_ef_construction=settings.hnsw_ef_construction,
                hnsw_ef_search=settings.hnsw_ef_search,
                namespace_index=settings.namespace_index,
                provision_projection=settings.provision_projection,
                create_extension=settings.create_extension,
            ),
            embedder_id=ctx.embedding.embedder_id,
        )
        await service.start()
        return BuildResult(
            service,
            ProviderRef("vector", "pgvector"),
            (sql_ref,),
            frozenset({"vector", "namespace_filter", "erase"}),
            health_probe=service.probe,
        )


def register() -> None:
    if "pgvector" in VECTOR_STORES:
        return
    VECTOR_STORES.register(
        "pgvector",
        ProviderDescriptor(
            ProviderRef("vector", "pgvector"),
            PgvectorSettings,
            frozenset({"vector", "namespace_filter", "erase"}),
            (
                DependencyRequirement("package", "pgvector"),
                DependencyRequirement("capability", "sql.postgres"),
            ),
            PgvectorBuilder(),
            service_manifest=CapabilityManifest("pgvector", "vector", extra="postgres"),
            requires_deployment_id=True,
            projection_settings=_projection_settings,
            locality="cluster",
        ),
    )
