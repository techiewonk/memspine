"""SQL factories own native connections; runtime only sees SQL capabilities."""

from __future__ import annotations

import hashlib
from pathlib import Path

from pydantic import BaseModel, JsonValue
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url

from memspine.config.schema.providers import PostgresSettings, SqliteSettings
from memspine.exceptions import ConfigError
from memspine.providers.contexts import StorageBuildContext
from memspine.providers.contracts import (
    DependencyRequirement,
    ProjectionPlacement,
    ProviderRef,
    StorageBuildResult,
)
from memspine.providers.health import sql_health_probe
from memspine.providers.registry import STORAGE_BACKENDS, ProviderDescriptor
from memspine.providers.sql import ManagedSqlAccess
from memspine.services.base import CapabilityManifest
from memspine.services.storage.sql_base import SqlStorage


def sqlite_placement(settings: BaseModel, owner_id: str) -> ProjectionPlacement:
    if not isinstance(settings, SqliteSettings):
        raise TypeError("sqlite placement requires SqliteSettings")
    memory = settings.path == ":memory:"
    return ProjectionPlacement(None if memory else Path(settings.path), memory, owner_id)


def postgres_placement(settings: BaseModel, owner_id: str) -> ProjectionPlacement:
    if not isinstance(settings, PostgresSettings):
        raise TypeError("postgres placement requires PostgresSettings")
    if not settings.data_dir:
        raise ConfigError("storage.settings.data_dir is required for derived projections")
    return ProjectionPlacement(Path(settings.data_dir) / "memspine", False, owner_id)


def postgres_target_identity(settings: BaseModel) -> dict[str, JsonValue]:
    """Hash the canonical full SQL URL without returning credentials or options."""
    if not isinstance(settings, PostgresSettings):
        raise TypeError("postgres target identity requires PostgresSettings")
    url = make_url(settings.url)
    canonical_url = url.set(
        drivername="postgresql",
        port=url.port or 5432,
        query=dict(sorted(url.query.items())),
    ).render_as_string(hide_password=False)
    return {
        "driver": "postgresql",
        "url_sha256": hashlib.sha256(canonical_url.encode("utf-8")).hexdigest(),
    }


class SqliteBuilder:
    async def build(
        self, settings: SqliteSettings, ctx: StorageBuildContext
    ) -> StorageBuildResult[SqlStorage]:
        from memspine.clients.sqlite import SQLiteClient
        from memspine.runtime.outbox import memory_engine, sqlite_engine
        from memspine.services.storage.sqlite.engine import SQLiteStorage

        key = settings.encryption.key_env if settings.encryption.mode == "sqlcipher" else None
        client = SQLiteClient(
            settings.path,
            cipher_key_env=key,
            busy_timeout_s=settings.busy_timeout_s,
        )
        ref = ctx.shared.resources.own("sql", client.close)
        await client.connect()
        access = ManagedSqlAccess(
            client.engine,
            is_memory=client.is_memory,
            capabilities=frozenset({"sql", "sql.sqlite", "transactions", "sync_sql"}),
            checkpoint=client.checkpoint,
            sync_factory=(
                memory_engine
                if client.is_memory
                else lambda: sqlite_engine(
                    client.path,
                    client.sync_creator,
                    busy_timeout_s=settings.busy_timeout_s,
                )
            ),
            owner_resource_id=ref.resource_id,
        )
        service = SQLiteStorage(client, mode=ctx.event_log.mode, compress=ctx.event_log.compress)
        return StorageBuildResult(
            service,
            ProviderRef("storage", "sqlite"),
            (ref,),
            access.capabilities,
            access,
            health_probe=lambda: sql_health_probe(access),
        )


class PostgresBuilder:
    async def build(
        self, settings: PostgresSettings, ctx: StorageBuildContext
    ) -> StorageBuildResult[SqlStorage]:
        from memspine.clients.postgres import (
            PostgresClient,
            driver_connect_timeout,
            normalize_pg_url,
        )
        from memspine.services.storage.postgres.engine import PostgresStorage

        url = (
            settings.url.get_secret_value()
            if hasattr(settings.url, "get_secret_value")
            else str(settings.url)
        )
        client = PostgresClient(
            url,
            connect_timeout_s=settings.connect_timeout_s,
            statement_timeout_s=settings.statement_timeout_s,
        )
        ref = ctx.shared.resources.own("sql", client.close)
        # The client enforces connect_timeout_s as the overall deadline (RM-40).
        await client.connect()

        async def checkpoint() -> None:
            # PostgreSQL durability/erasure is verified by SQL operations; no SQLite WAL exists.
            return None

        access = ManagedSqlAccess(
            client.engine,
            is_memory=False,
            capabilities=frozenset(
                {"sql", "sql.postgres", "sql.shared_leases", "transactions", "sync_sql"}
            ),
            checkpoint=checkpoint,
            sync_factory=lambda: create_engine(
                normalize_pg_url(url),
                connect_args={
                    # Sync (Alembic) connects are bounded per attempt only (RM-40).
                    "connect_timeout": driver_connect_timeout(settings.connect_timeout_s),
                    "options": f"-c statement_timeout={max(1, int(settings.statement_timeout_s * 1000))}",
                },
            ),
            owner_resource_id=ref.resource_id,
        )
        service = PostgresStorage(
            client,
            mode=ctx.event_log.mode,
            compress=ctx.event_log.compress,
            use_alembic=ctx.profile == "server",
        )
        return StorageBuildResult(
            service,
            ProviderRef("storage", "postgres"),
            (ref,),
            access.capabilities,
            access,
            health_probe=lambda: sql_health_probe(access),
        )


def register() -> None:
    for name, model, builder, placement, dependencies, capabilities in (
        (
            "sqlite",
            SqliteSettings,
            SqliteBuilder(),
            sqlite_placement,
            (),
            {"sql", "sql.sqlite", "transactions", "sync_sql"},
        ),
        (
            "postgres",
            PostgresSettings,
            PostgresBuilder(),
            postgres_placement,
            (DependencyRequirement("package", "psycopg"),),
            {"sql", "sql.postgres", "sql.shared_leases", "transactions", "sync_sql"},
        ),
    ):
        if name not in STORAGE_BACKENDS:
            STORAGE_BACKENDS.register(
                name,
                ProviderDescriptor(
                    ProviderRef("storage", name),
                    model,
                    frozenset(capabilities),
                    dependencies,
                    builder,
                    service_manifest=(
                        CapabilityManifest(name, "sql", extra="postgres")
                        if name == "postgres"
                        else None
                    ),
                    placement=placement,
                    requires_deployment_id=name == "postgres",
                    target_identity=(postgres_target_identity if name == "postgres" else None),
                ),
            )
