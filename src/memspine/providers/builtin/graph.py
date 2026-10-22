"""Primary graph factories; direct Neo4j is distinct from an outbox mirror."""

from __future__ import annotations

import os
from hashlib import sha256
from urllib.parse import urlsplit

from pydantic import BaseModel, JsonValue

from memspine.config.schema.providers import LadybugSettings, SqlAdjacencySettings
from memspine.exceptions import ConfigError
from memspine.providers.contexts import GraphBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.placement import generation_component
from memspine.providers.registry import GRAPH_STORES, ProviderDescriptor
from memspine.providers.settings import Neo4jSettings
from memspine.providers.shared import own_shared, path_identity, resource_key
from memspine.services.base import CapabilityManifest
from memspine.services.graph.base import GraphStore


def _neo4j_target_identity(settings: BaseModel) -> dict[str, JsonValue]:
    """Resolve endpoint identity without retaining credentials or raw URL data."""
    if not isinstance(settings, Neo4jSettings):
        raise ConfigError("Neo4j target identity received incompatible settings")
    raw_url = os.environ.get(settings.url_env)
    if not raw_url:
        raise ConfigError(f"Neo4j target needs environment variable {settings.url_env}")
    try:
        parsed = urlsplit(raw_url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError:
        raise ConfigError("Neo4j target URL is invalid") from None
    if not parsed.scheme or not hostname:
        raise ConfigError("Neo4j target URL is invalid")
    username = os.environ.get(settings.user_env) or ""
    password = os.environ.get(settings.password_env) or ""
    auth_material = "\0".join((username, password, parsed.username or "", parsed.password or ""))
    auth_digest = sha256(auth_material.encode()).hexdigest()
    return {
        "scheme": parsed.scheme.lower(),
        "host": hostname.lower(),
        "port": port,
        "database": settings.database,
        "url_digest": sha256(raw_url.encode()).hexdigest(),
        "auth_digest": auth_digest,
        # Physical target identity intentionally excludes credentials and transport
        # settings. Two roles with different keys must not mirror into one target.
        "canonical_target": {
            "kind": "neo4j",
            "host": hostname.lower(),
            "port": port or 7687,
            "database": settings.database,
        },
    }


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
            frozenset({"graph", "namespace_filter", "erase", "qualifier_filter"}),
            health_probe=probe,
        )


class LadybugBuilder:
    async def build(
        self, settings: LadybugSettings, ctx: GraphBuildContext
    ) -> BuildResult[GraphStore]:
        from memspine.clients.ladybug import LadybugClient
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
            else f"{ctx.placement.base_path}.ladybug{generation_suffix}"
        )
        service: LadybugGraphStore
        if path == ":memory:":
            client = LadybugClient(path)
            ref = ctx.shared.resources.own(ctx.resource_id, client.close)
            await client.connect()
            service = LadybugGraphStore(client)
        else:

            async def open_store() -> tuple[LadybugClient, LadybugGraphStore]:
                opened = LadybugClient(path)
                await opened.connect()
                return opened, LadybugGraphStore(opened)

            async def close_store(handle: tuple[LadybugClient, LadybugGraphStore]) -> None:
                await handle[0].close()

            # Single-writer embedded database (ADR-079): one Database/Connection and one
            # store (whose statement lock is a threading lock) per path per process.
            (_client, service), ref = await own_shared(
                ctx.shared.resources,
                ctx.resource_id,
                resource_key("graph.ladybug", {"path": path_identity(path)}, scope="thread"),
                open_store,
                close=close_store,
                registry=ctx.shared.resource_registry(),
            )

        async def probe() -> bool:
            await service.node_count()
            return True

        return BuildResult(
            service,
            ProviderRef("graph", "ladybug"),
            (ref,),
            frozenset({"graph", "namespace_filter", "erase", "qualifier_filter"}),
            health_probe=probe,
        )


class Neo4jBuilder:
    async def build(
        self, settings: Neo4jSettings, ctx: GraphBuildContext
    ) -> BuildResult[GraphStore]:
        from memspine.clients.neo4j import Neo4jClient
        from memspine.services.graph.neo4j import Neo4jGraphStore

        if not ctx.shared.owner_id:
            raise ConfigError("Neo4j needs an explicit stable runtime.deployment_id")
        scope = sha256(
            f"{ctx.shared.owner_id}:{ctx.resource_id}:{ctx.generation_id}".encode()
        ).hexdigest()[:24]
        from memspine.services.graph.neo4j import DriverExecutor

        http = ctx.shared.http_pool
        client = Neo4jClient(
            settings,
            executor_factory=lambda driver: DriverExecutor(
                driver,
                database=settings.database,
                node_label=f"MemNode_{scope}",
                rel_type=f"MemRel_{scope}",
            ),
            max_connections=http.max_connections if http is not None else None,
            keepalive_s=http.keepalive_s if http is not None else None,
        )
        ref = ctx.shared.resources.own(ctx.resource_id, client.close)
        await client.connect()
        service = Neo4jGraphStore(client.executor, scoped=True)

        async def probe() -> bool:
            await service.node_count()
            return True

        return BuildResult(
            service,
            ProviderRef("graph", "neo4j"),
            (ref,),
            frozenset({"graph", "namespace_filter", "erase", "mirror", "qualifier_filter"}),
            health_probe=probe,
        )


def register() -> None:
    for name, model, builder, requirements, capabilities in (
        (
            "sql_adjacency",
            SqlAdjacencySettings,
            AdjacencyBuilder(),
            (),
            {"graph", "namespace_filter", "erase", "qualifier_filter"},
        ),
        (
            "ladybug",
            LadybugSettings,
            LadybugBuilder(),
            (DependencyRequirement("package", "ladybug"),),
            {"graph", "namespace_filter", "erase", "qualifier_filter"},
        ),
        (
            "neo4j",
            Neo4jSettings,
            Neo4jBuilder(),
            (DependencyRequirement("package", "neo4j"),),
            {"graph", "namespace_filter", "erase", "mirror", "qualifier_filter"},
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
                ),
            )
