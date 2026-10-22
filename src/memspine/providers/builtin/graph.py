"""Primary graph factories; direct Neo4j is distinct from an outbox mirror."""

from __future__ import annotations

from hashlib import sha256
from typing import cast

from pydantic import BaseModel, JsonValue

from memspine.clients.neo4j import Neo4jClient, Neo4jSpec
from memspine.config.schema.providers import LadybugSettings, SqlAdjacencySettings
from memspine.config.schema.runtime import SharedHttpPoolConfig
from memspine.exceptions import ConfigError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import GraphBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.placement import generation_component, host_base_path
from memspine.providers.registry import (
    GRAPH_STORES,
    ProviderDescriptor,
    placed_locality,
    records_locality,
)
from memspine.providers.settings import Neo4jSettings
from memspine.services.base import CapabilityManifest
from memspine.services.graph.base import GraphStore


def neo4j_spec(settings: Neo4jSettings, http: SharedHttpPoolConfig | None = None) -> Neo4jSpec:
    """The Neo4j connector spec of one graph slot (pool size from the Engine's HTTP pool)."""
    return Neo4jSpec(
        connect_timeout_s=settings.connect_timeout_s,
        request_timeout_s=settings.request_timeout_s,
        url_env=settings.url_env,
        user_env=settings.user_env,
        password_env=settings.password_env,
        database=settings.database,
        max_connections=http.max_connections if http is not None else None,
        keepalive_s=http.keepalive_s if http is not None else None,
    )


def _neo4j_target_identity(settings: BaseModel) -> dict[str, JsonValue]:
    """The connector's identity (the one parser): endpoint, auth digest and physical target,
    without credentials or the raw URL; byte-identical to the pre-connector descriptor."""
    if not isinstance(settings, Neo4jSettings):
        raise ConfigError("Neo4j target identity received incompatible settings")
    return cast(
        dict[str, JsonValue], Neo4jClient.identity(neo4j_spec(settings)).descriptor_target()
    )


class AdjacencyBuilder:
    async def build(
        self, settings: SqlAdjacencySettings, ctx: GraphBuildContext
    ) -> BuildResult[GraphStore]:
        from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph

        if ctx.sql is None or not ({"sql.sqlite", "sql.postgres"} & ctx.sql.capabilities):
            raise ConfigError(
                "sql_adjacency requires a compatible SQL records store (SQLite or Postgres)"
            )
        if ctx.generation_id != "active":
            raise ConfigError(
                "controlled graph switching is unsupported for sql_adjacency because it shares the authoritative SQL database"
            )
        ref = ctx.shared.resources.borrow("sql")
        service = SqlAdjacencyGraph(ctx.sql)

        async def probe() -> bool:
            await service.node_count()
            return True

        return BuildResult(
            service,
            ProviderRef("graph", "sql_adjacency"),
            (ref,),
            frozenset({"graph", "namespace_filter", "erase"}),
            health_probe=probe,
        )


class LadybugBuilder:
    async def build(
        self, settings: LadybugSettings, ctx: GraphBuildContext
    ) -> BuildResult[GraphStore]:
        from memspine.clients.ladybug import LadybugClient, LadybugSpec
        from memspine.services.graph.ladybug import LadybugGraphStore

        if ctx.placement.in_memory and ctx.generation_id != "active":
            raise ConfigError("controlled graph generations require durable storage placement")
        generation_suffix = (
            ""
            if ctx.generation_id == "active"
            else f".generation-{generation_component(ctx.generation_id)}"
        )
        path = (
            ":memory:"
            if ctx.placement.in_memory
            else f"{host_base_path(ctx.placement, 'graph', 'ladybug')}.ladybug{generation_suffix}"
        )
        client: LadybugClient
        if path == ":memory:":
            client = LadybugClient(LadybugSpec(path))
            ref = ctx.shared.resources.own(ctx.resource_id, client.close)
            await client.open()
        else:
            # Single-writer embedded database (ADR-079): one Database/Connection per path per
            # process, shared through the connector registry. The connector carries the
            # statement lock (a threading lock), so every Engine's store serialises on it.
            client, ref = await acquire(
                ctx.shared, LadybugClient, LadybugSpec(path), resource_id=ctx.resource_id
            )
        service = LadybugGraphStore(client)

        async def probe() -> bool:
            await service.node_count()
            return True

        return BuildResult(
            service,
            ProviderRef("graph", "ladybug"),
            (ref,),
            frozenset({"graph", "namespace_filter", "erase"}),
            health_probe=probe,
        )


class Neo4jBuilder:
    async def build(
        self, settings: Neo4jSettings, ctx: GraphBuildContext
    ) -> BuildResult[GraphStore]:
        from memspine.services.graph.neo4j import Neo4jGraphStore, connector_executor

        if not ctx.shared.owner_id:
            raise ConfigError("Neo4j needs an explicit stable deployment.id")
        scope = sha256(
            f"{ctx.shared.owner_id}:{ctx.resource_id}:{ctx.generation_id}".encode()
        ).hexdigest()[:24]
        # RM-113 item 6: one driver per target, credentials and transport knobs, shared by the
        # primary graph and the mirror of every Engine in the process (ADR-079). The executor
        # never closes the driver; the registry closes it on the last release.
        client, ref = await acquire(
            ctx.shared,
            Neo4jClient,
            neo4j_spec(settings, ctx.shared.http_pool),
            resource_id=ctx.resource_id,
        )
        executor = connector_executor(
            client, node_label=f"MemNode_{scope}", rel_type=f"MemRel_{scope}"
        )
        service = Neo4jGraphStore(executor, scoped=True)

        async def probe() -> bool:
            await service.node_count()
            return True

        return BuildResult(
            service,
            ProviderRef("graph", "neo4j"),
            (ref,),
            frozenset({"graph", "namespace_filter", "erase", "mirror"}),
            health_probe=probe,
        )


def register() -> None:
    for name, model, builder, requirements, capabilities in (
        (
            "sql_adjacency",
            SqlAdjacencySettings,
            AdjacencyBuilder(),
            (),
            {"graph", "namespace_filter", "erase"},
        ),
        (
            "ladybug",
            LadybugSettings,
            LadybugBuilder(),
            (DependencyRequirement("package", "ladybug"),),
            {"graph", "namespace_filter", "erase"},
        ),
        (
            "neo4j",
            Neo4jSettings,
            Neo4jBuilder(),
            (DependencyRequirement("package", "neo4j"),),
            {"graph", "namespace_filter", "erase", "mirror"},
        ),
    ):
        if name not in GRAPH_STORES:
            GRAPH_STORES.register(
                name,
                ProviderDescriptor(
                    ProviderRef("graph", name),
                    model,
                    frozenset(capabilities),
                    requirements,
                    builder,
                    service_manifest=(
                        CapabilityManifest(name, "graph", extra="graph")
                        if name == "ladybug"
                        else CapabilityManifest(name, "graph", extra="neo4j")
                        if name == "neo4j"
                        else None
                    ),
                    target_identity=_neo4j_target_identity if name == "neo4j" else None,
                    requires_deployment_id=name == "neo4j",
                    # sql_adjacency lives inside the records SQL database, so its effective
                    # locality is the records slot's (cluster on Postgres, host on a SQLite
                    # file, process for :memory:); WP6 resolves it through the records slot.
                    locality="cluster" if name == "neo4j" else "host",
                    resolve_locality=(
                        records_locality
                        if name == "sql_adjacency"
                        else placed_locality
                        if name == "ladybug"
                        else None
                    ),
                    single_writer=name == "ladybug",
                    needs_placement=name == "ladybug",
                ),
            )
