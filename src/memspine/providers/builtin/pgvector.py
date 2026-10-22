"""Built-in pgvector descriptor and storage-owned SQL lease integration."""

from __future__ import annotations

import hashlib
import json

from pydantic import BaseModel, JsonValue
from sqlalchemy import Column, Index, MetaData, String, Table, inspect, text
from sqlalchemy.engine import Engine

from memspine.config.schema.providers import PgvectorSettings
from memspine.exceptions import ConfigError, MissingServiceError
from memspine.providers.async_utils import run_blocking_drained
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
                "pgvector does not support embedding.versioned_spaces; select a vector provider "
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
            from pgvector.sqlalchemy import VECTOR
        except ImportError:
            raise MissingServiceError("vector:pgvector", "postgres") from None

        sql_ref = ctx.shared.resources.borrow("sql")
        lease = ctx.sql.sync_lease(ctx.shared.resources, "pgvector")
        table_name = _table_name(ctx)
        metadata = MetaData(schema="memspine_vectors")
        table = Table(
            table_name,
            metadata,
            Column("record_id", String(512), primary_key=True),
            Column("namespace", String(512), nullable=False),
            Column("embedder_id", String(512), nullable=False),
            Column("embedding", VECTOR(ctx.embedding.dim), nullable=False),
        )
        if settings.namespace_index:
            Index(f"ns_{table_name}", table.c.namespace)
        if settings.index == "hnsw":
            Index(
                f"hnsw_{table_name}",
                table.c.embedding,
                postgresql_using="hnsw",
                postgresql_with={
                    "m": settings.hnsw_m,
                    "ef_construction": settings.hnsw_ef_construction,
                },
                postgresql_ops={"embedding": "vector_cosine_ops"},
            )

        def initialize() -> None:
            with lease.engine.begin() as connection:
                has_extension = bool(
                    connection.scalar(
                        text("SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'vector')")
                    )
                )
                if not has_extension and settings.create_extension:
                    connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                    has_extension = True
                if not has_extension:
                    raise ConfigError(
                        "the selected PostgreSQL database does not have the pgvector extension; "
                        "install it or explicitly set storage.vector.settings.create_extension=true"
                    )
                if settings.provision_projection:
                    connection.execute(text("CREATE SCHEMA IF NOT EXISTS memspine_vectors"))
            if settings.provision_projection:
                metadata.create_all(lease.engine)
                return
            inspector = inspect(lease.engine)
            if not inspector.has_table(table_name, schema="memspine_vectors"):
                raise ConfigError(
                    "the owned pgvector projection table is missing; explicitly set "
                    "storage.vector.settings.provision_projection=true during operator provisioning"
                )
            columns = inspector.get_columns(table_name, schema="memspine_vectors")
            by_name = {str(column["name"]): column["type"] for column in columns}
            embedding_type = by_name.get("embedding")
            if (
                not {"record_id", "namespace", "embedder_id", "embedding"}.issubset(by_name)
                or not isinstance(embedding_type, VECTOR)
                or embedding_type.dim != ctx.embedding.dim
            ):
                raise ConfigError(
                    "the existing pgvector projection table does not match the selected "
                    "embedding schema"
                )

        await run_blocking_drained(initialize)

        from memspine.services.vector.pgvector_store import PgVectorStore

        service = PgVectorStore(
            lease.engine,
            table,
            embedder_id=ctx.embedding.embedder_id,
            dimension=ctx.embedding.dim,
            hnsw_ef_search=settings.hnsw_ef_search,
            use_hnsw=settings.index == "hnsw",
        )

        async def probe() -> bool:
            return await run_blocking_drained(_probe, lease.engine, table)

        return BuildResult(
            service,
            ProviderRef("vector", "pgvector"),
            (sql_ref, lease.resource),
            frozenset({"vector", "namespace_filter", "erase"}),
            health_probe=probe,
        )


def _probe(engine: Engine, table: Table) -> bool:
    from sqlalchemy import select

    with engine.connect() as connection:
        return connection.execute(select(table.c.record_id).limit(0)).returns_rows


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
