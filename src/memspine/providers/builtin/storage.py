"""SQL factories own native connections; runtime only sees SQL capabilities."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, JsonValue
from sqlalchemy import Engine
from sqlalchemy.engine import make_url

from memspine.config.schema.providers import PostgresSettings, SqliteSettings
from memspine.exceptions import ConfigError
from memspine.providers.async_utils import run_blocking_drained
from memspine.providers.contexts import StorageBuildContext
from memspine.providers.contracts import (
    DependencyRequirement,
    ProjectionPlacement,
    ProviderRef,
    StorageBuildResult,
)
from memspine.providers.health import sql_health_probe
from memspine.providers.registry import STORAGE_BACKENDS, ProviderDescriptor
from memspine.providers.shared import own_shared, path_identity, resource_key
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


def sqlite_shared_identity(
    settings: SqliteSettings,
) -> tuple[dict[str, JsonValue], dict[str, JsonValue]]:
    """Credential-free registry identity of a file SQLite database (ADR-079).

    The SQLCipher passphrase enters only as a digest, so a different key never shares
    a pool with the right one."""
    key_env = settings.encryption.key_env if settings.encryption.mode == "sqlcipher" else None
    key_digest = (
        hashlib.sha256(os.environ.get(key_env, "").encode("utf-8")).hexdigest() if key_env else None
    )
    target: dict[str, JsonValue] = {"kind": "sqlite", "path": path_identity(settings.path)}
    knobs: dict[str, JsonValue] = {
        "busy_timeout_s": settings.busy_timeout_s,
        "encryption": settings.encryption.mode,
        "key_env": key_env,
        "key_digest": key_digest,
    }
    return target, knobs


def sql_pool_options(ctx: StorageBuildContext) -> dict[str, Any]:
    """SQLAlchemy queue-pool keyword arguments from ``runtime.shared_resources.sql`` (RM-78);
    empty for a direct build without an Engine (library defaults)."""
    pool = ctx.shared.sql_pool
    if pool is None:
        return {}
    return {
        "pool_size": pool.pool_size,
        "max_overflow": pool.max_overflow,
        "pool_recycle": pool.pool_recycle_s,
        "pool_pre_ping": pool.pool_pre_ping,
    }


async def _close_client(client: Any) -> None:
    await client.close()


async def _dispose_sync(engine: Engine) -> None:
    await run_blocking_drained(engine.dispose)


class SqliteBuilder:
    async def build(
        self, settings: SqliteSettings, ctx: StorageBuildContext
    ) -> StorageBuildResult[SqlStorage]:
        from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
        from memspine.clients.sqlite.sync import memory_sync_engine
        from memspine.services.storage.sqlite.engine import SQLiteStorage

        key = settings.encryption.key_env if settings.encryption.mode == "sqlcipher" else None
        resources = ctx.shared.resources
        capabilities = frozenset({"sql", "sql.sqlite", "transactions", "sync_sql"})
        if settings.path == ":memory:":
            # Unique per Engine (a private shared-cache name); never registered for sharing.
            client = SQLiteClient(
                SQLiteSpec(
                    settings.path, cipher_key_env=key, busy_timeout_s=settings.busy_timeout_s
                )
            )
            ref = resources.own("sql", client.close)
            await client.open()
            access = ManagedSqlAccess(
                client.engine,
                is_memory=True,
                capabilities=capabilities,
                checkpoint=client.checkpoint,
                sync_factory=memory_sync_engine,
                owner_resource_id=ref.resource_id,
            )
        else:
            target, knobs = sqlite_shared_identity(settings)
            # SQLCipher opens through one keyed connection (fixed pool): pool options are
            # ignored there and stay out of the key.
            pool = {} if key is not None else sql_pool_options(ctx)
            knobs = {**knobs, "pool": cast(JsonValue, pool)}

            async def open_client() -> SQLiteClient:
                opened = SQLiteClient(
                    SQLiteSpec(
                        settings.path,
                        cipher_key_env=key,
                        busy_timeout_s=settings.busy_timeout_s,
                        pool_options=pool,
                    )
                )
                await opened.open()
                return opened

            client, ref = await own_shared(
                resources,
                "sql",
                resource_key("sql.sqlite.async", target, knobs, scope="loop"),
                open_client,
                close=_close_client,
                registry=ctx.shared.resource_registry(),
            )
            shared_client = client

            async def open_sync() -> Engine:
                return shared_client.sync_engine(pool)

            sync_engine, sync_ref = await own_shared(
                resources,
                "sql:sync",
                resource_key("sql.sqlite.sync", target, knobs, scope="thread"),
                open_sync,
                close=_dispose_sync,
                registry=ctx.shared.resource_registry(),
                depends_on=(ref.resource_id,),
            )
            access = ManagedSqlAccess(
                client.engine,
                is_memory=False,
                capabilities=capabilities,
                checkpoint=client.checkpoint,
                sync_engine=sync_engine,
                sync_resource_id=sync_ref.resource_id,
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
            PostgresSpec,
        )
        from memspine.services.storage.postgres.engine import PostgresStorage

        url = (
            settings.url.get_secret_value()
            if hasattr(settings.url, "get_secret_value")
            else str(settings.url)
        )
        resources = ctx.shared.resources
        target = postgres_target_identity(settings)
        knobs: dict[str, JsonValue] = {
            "connect_timeout_s": settings.connect_timeout_s,
            "statement_timeout_s": settings.statement_timeout_s,
        }
        pool = sql_pool_options(ctx)
        knobs["pool"] = cast(JsonValue, pool)

        async def open_client() -> PostgresClient:
            opened = PostgresClient(
                PostgresSpec(
                    url,
                    connect_timeout_s=settings.connect_timeout_s,
                    statement_timeout_s=settings.statement_timeout_s,
                    pool_options=pool,
                )
            )
            # The client enforces connect_timeout_s as the overall deadline (RM-40).
            await opened.open()
            return opened

        client, ref = await own_shared(
            resources,
            "sql",
            resource_key("sql.postgres.async", target, knobs, scope="loop"),
            open_client,
            close=_close_client,
            registry=ctx.shared.resource_registry(),
        )

        async def open_sync() -> Engine:
            # One sync pool per database for every purpose (outbox, pgvector, sleep leases);
            # sync connects are bounded per attempt only (RM-40).
            engine: Engine = client.sync_engine(pool)
            return engine

        sync_engine, sync_ref = await own_shared(
            resources,
            "sql:sync",
            resource_key("sql.postgres.sync", target, knobs, scope="thread"),
            open_sync,
            close=_dispose_sync,
            registry=ctx.shared.resource_registry(),
            depends_on=(ref.resource_id,),
        )

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
            sync_engine=sync_engine,
            sync_resource_id=sync_ref.resource_id,
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
