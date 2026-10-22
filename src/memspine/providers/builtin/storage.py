"""SQL factories own native connections; runtime only sees SQL capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from pydantic import BaseModel, JsonValue
from sqlalchemy import Engine

from memspine.clients.base import Connector
from memspine.clients.postgres import PostgresClient, PostgresSpec
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.config.schema.providers import PostgresSettings, SqliteSettings
from memspine.providers.async_utils import run_blocking_drained
from memspine.providers.connectors import acquire
from memspine.providers.contexts import StorageBuildContext
from memspine.providers.contracts import (
    DependencyRequirement,
    ProjectionPlacement,
    ProviderRef,
    ResourceRef,
    StorageBuildResult,
)
from memspine.providers.health import sql_health_probe
from memspine.providers.registry import STORAGE_BACKENDS, Locality, ProviderDescriptor
from memspine.providers.shared import own_shared, resource_key
from memspine.providers.sql import ManagedSqlAccess
from memspine.services.base import CapabilityManifest
from memspine.services.storage.sql_base import SqlStorage


def sqlite_placement(
    settings: BaseModel, owner_id: str, data_dir: str | None = None
) -> ProjectionPlacement:
    """Host-local projections sit beside the database file (``<path>.lance`` etc.).

    ``deployment.data_dir`` (ADR-081) defaults to the database file's directory, so an
    unset value keeps today's base path and projection fingerprints byte-identical."""
    if not isinstance(settings, SqliteSettings):
        raise TypeError("sqlite placement requires SqliteSettings")
    memory = settings.path == ":memory:"
    if memory:
        return ProjectionPlacement(None, True, owner_id)
    path = Path(settings.path)
    base = path if data_dir is None else Path(data_dir) / path.name
    return ProjectionPlacement(base, False, owner_id)


def sqlite_locality(settings: BaseModel, _records: Locality) -> Locality:
    """ADR-081 WP6: ``:memory:`` lives in this process; a database file lives on this host."""
    if not isinstance(settings, SqliteSettings):
        raise TypeError("sqlite locality requires SqliteSettings")
    return "process" if settings.path == ":memory:" else "host"


def postgres_placement(
    settings: BaseModel, owner_id: str, data_dir: str | None = None
) -> ProjectionPlacement:
    """A network database has no file to sit beside: host-local projections live under
    ``deployment.data_dir/memspine`` (the pre-ADR-081 ``settings.data_dir`` base). Without
    ``data_dir`` the base is None, and only a host-local slot asks for it."""
    if not isinstance(settings, PostgresSettings):
        raise TypeError("postgres placement requires PostgresSettings")
    base = None if data_dir is None else Path(data_dir) / "memspine"
    return ProjectionPlacement(base, False, owner_id)


def _postgres_url(settings: PostgresSettings) -> str:
    return (
        settings.url.get_secret_value()
        if hasattr(settings.url, "get_secret_value")
        else str(settings.url)
    )


def postgres_spec(settings: PostgresSettings, pool: dict[str, Any] | None = None) -> PostgresSpec:
    """The Postgres connector spec of one records slot (``pool`` = queue-pool options)."""
    return PostgresSpec(
        _postgres_url(settings),
        connect_timeout_s=settings.connect_timeout_s,
        statement_timeout_s=settings.statement_timeout_s,
        pool_options=pool or {},
    )


def postgres_target_identity(settings: BaseModel) -> dict[str, JsonValue]:
    """The connector's target (the SHA-256 of the canonical full SQL URL), without
    credentials or options: byte-identical to the pre-connector descriptor identity."""
    if not isinstance(settings, PostgresSettings):
        raise TypeError("postgres target identity requires PostgresSettings")
    return cast(dict[str, JsonValue], dict(PostgresClient.identity(postgres_spec(settings)).target))


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


async def _dispose_sync(engine: Engine) -> None:
    await run_blocking_drained(engine.dispose)


async def _shared_sync_engine(
    ctx: StorageBuildContext,
    connector_cls: type[Connector[Any]],
    spec: Any,
    client: SQLiteClient | PostgresClient,
    pool: dict[str, Any],
    owner_ref: ResourceRef,
) -> tuple[Engine, ResourceRef]:
    """One sync pool per database for every purpose (outbox, pgvector, sleep leases): a
    second registry lease keyed by the connector's identity, lent by the connector and
    closed after every purpose label and before the async connector (ADR-079)."""
    identity = connector_cls.identity(spec)

    async def open_sync() -> Engine:
        # Sync connects are bounded per attempt only (RM-40).
        engine: Engine = client.sync_engine(pool)
        return engine

    return await own_shared(
        ctx.shared.resources,
        "sql:sync",
        resource_key(
            f"{identity.kind}.sync", identity.key_target(), identity.knobs, scope="thread"
        ),
        open_sync,
        close=_dispose_sync,
        registry=ctx.shared.resource_registry(),
        depends_on=(owner_ref.resource_id,),
    )


class SqliteBuilder:
    async def build(
        self, settings: SqliteSettings, ctx: StorageBuildContext
    ) -> StorageBuildResult[SqlStorage]:
        from memspine.clients.sqlite.sync import memory_sync_engine
        from memspine.services.storage.sqlite.engine import SQLiteStorage

        key = settings.encryption.key_env if settings.encryption.mode == "sqlcipher" else None
        resources = ctx.shared.resources
        capabilities = frozenset({"sql", "sql.sqlite", "transactions", "sync_sql"})
        client: SQLiteClient
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
                connector=client,
            )
        else:
            # SQLCipher opens through one keyed connection (fixed pool): pool options are
            # ignored there and stay out of the key.
            pool = {} if key is not None else sql_pool_options(ctx)
            spec = SQLiteSpec(
                settings.path,
                cipher_key_env=key,
                busy_timeout_s=settings.busy_timeout_s,
                pool_options=pool,
            )
            client, ref = await acquire(ctx.shared, SQLiteClient, spec, resource_id="sql")
            sync_engine, sync_ref = await _shared_sync_engine(
                ctx, SQLiteClient, spec, client, pool, ref
            )
            access = ManagedSqlAccess(
                client.engine,
                is_memory=False,
                capabilities=capabilities,
                checkpoint=client.checkpoint,
                sync_engine=sync_engine,
                sync_resource_id=sync_ref.resource_id,
                owner_resource_id=ref.resource_id,
                connector=client,
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
        from memspine.services.storage.postgres.engine import PostgresStorage

        pool = sql_pool_options(ctx)
        spec = postgres_spec(settings, pool)
        # The connector enforces connect_timeout_s as the overall open deadline (RM-40).
        client, ref = await acquire(ctx.shared, PostgresClient, spec, resource_id="sql")
        sync_engine, sync_ref = await _shared_sync_engine(
            ctx, PostgresClient, spec, client, pool, ref
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
            connector=client,
        )
        service = PostgresStorage(
            client,
            mode=ctx.event_log.mode,
            compress=ctx.event_log.compress,
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
                    locality="cluster" if name == "postgres" else "host",
                    resolve_locality=sqlite_locality if name == "sqlite" else None,
                ),
            )
