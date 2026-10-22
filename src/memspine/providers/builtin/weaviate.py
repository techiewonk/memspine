"""Weaviate vector and lexical provider descriptors."""

from __future__ import annotations

from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, JsonValue

from memspine.clients.weaviate import WeaviateSpec
from memspine.config.schema.remote_providers import WeaviateSettings
from memspine.exceptions import ConfigError
from memspine.providers.connectors import acquire
from memspine.providers.contexts import (
    LexicalBuildContext,
    SharedBuildContext,
    VectorBuildContext,
)
from memspine.providers.contracts import (
    BuildResult,
    DependencyRequirement,
    ProviderRef,
    ResourceRef,
)
from memspine.providers.registry import LEXICAL_INDEXES, VECTOR_STORES, ProviderDescriptor
from memspine.services._weaviate import (
    LEXICAL_PROPERTIES,
    VECTOR_PROPERTIES,
    WeaviateCollectionBinding,
    collection_name,
)
from memspine.services.base import CapabilityManifest
from memspine.services.lexical.base import LexicalStore
from memspine.services.vector.base import VectorStore

if TYPE_CHECKING:
    from memspine.clients.weaviate import WeaviateClient


def _spec(settings: WeaviateSettings, max_connections: int | None = None) -> WeaviateSpec:
    return WeaviateSpec(
        connect_timeout_s=settings.connect_timeout_s,
        request_timeout_s=settings.request_timeout_s,
        url_env=settings.url_env,
        grpc_url_env=settings.grpc_url_env,
        api_key_env=settings.api_key_env,
        grpc_port=settings.grpc_port,
        max_connections=max_connections,
    )


def _target_identity(model: BaseModel) -> dict[str, JsonValue]:
    """The connector's identity (the one parser): target, auth digest, physical target."""
    if not isinstance(model, WeaviateSettings):
        raise ConfigError("Weaviate target identity received incompatible settings")
    from memspine.clients.weaviate import WeaviateClient

    return cast(dict[str, JsonValue], WeaviateClient.identity(_spec(model)).descriptor_target())


async def _shared_client(
    settings: WeaviateSettings, shared: SharedBuildContext, resource_id: str
) -> tuple[WeaviateClient, ResourceRef]:
    """One connected client per Weaviate target and transport settings (ADR-079).

    The vector and lexical services of one Engine (and Engines in this process on the same
    loop) share it; each service still ensures its own collection. The key holds only the
    credential-free target identity and an auth digest."""
    from memspine.clients.weaviate import WeaviateClient

    pool_size = shared.http_pool.max_connections if shared.http_pool is not None else None
    return await acquire(
        shared, WeaviateClient, _spec(settings, pool_size), resource_id=resource_id
    )


class WeaviateVectorBuilder:
    async def build(
        self, settings: WeaviateSettings, ctx: VectorBuildContext
    ) -> BuildResult[VectorStore]:
        from memspine.services.vector.weaviate_store import WeaviateVectorStore

        if not ctx.shared.owner_id:
            raise ConfigError("Weaviate requires a stable deployment.id")
        if ctx.versioned_spaces:
            raise ConfigError("Weaviate does not support versioned embedding spaces")
        if ctx.quantization is not None or ctx.matryoshka_dim is not None:
            raise ConfigError(
                "Weaviate does not support configured quantization or Matryoshka rescoring"
            )
        vector_space = f"{ctx.generation_id}:{ctx.embedding.embedder_id}:{ctx.embedding.dim}"
        name = collection_name(
            settings.collection_prefix, "Vector", ctx.shared.owner_id, vector_space
        )
        client, ref = await _shared_client(settings, ctx.shared, "vector")
        service = WeaviateVectorStore(
            client,
            WeaviateCollectionBinding(name, VECTOR_PROPERTIES),
            owner_id=ctx.shared.owner_id,
            embedder_id=ctx.embedding.embedder_id,
            dimension=ctx.embedding.dim,
        )
        await service.start()

        async def probe() -> bool:
            return await service.probe(settings.request_timeout_s)

        return BuildResult(
            cast(VectorStore, service),
            ProviderRef("vector", "weaviate"),
            (ref,),
            frozenset({"vector", "namespace_filter", "erase"}),
            health_probe=probe,
        )


class WeaviateLexicalBuilder:
    async def build(
        self, settings: WeaviateSettings, ctx: LexicalBuildContext
    ) -> BuildResult[LexicalStore]:
        from memspine.services.lexical.weaviate_store import WeaviateLexicalStore

        if not ctx.shared.owner_id:
            raise ConfigError("Weaviate requires a stable deployment.id")
        name = collection_name(
            settings.collection_prefix, "Lexical", ctx.shared.owner_id, ctx.generation_id
        )
        client, ref = await _shared_client(settings, ctx.shared, "lexical")
        service = WeaviateLexicalStore(
            client,
            WeaviateCollectionBinding(name, LEXICAL_PROPERTIES),
            owner_id=ctx.shared.owner_id,
        )
        await service.start()

        async def probe() -> bool:
            return await service.probe(settings.request_timeout_s)

        return BuildResult(
            cast(LexicalStore, service),
            ProviderRef("lexical", "weaviate"),
            (ref,),
            frozenset({"lexical", "namespace_filter", "erase"}),
            health_probe=probe,
        )


def register() -> None:
    requirement = (DependencyRequirement("package", "weaviate"),)
    if "weaviate" not in VECTOR_STORES:
        VECTOR_STORES.register(
            "weaviate",
            ProviderDescriptor(
                ProviderRef("vector", "weaviate"),
                WeaviateSettings,
                frozenset({"vector", "namespace_filter", "erase"}),
                requirement,
                WeaviateVectorBuilder(),
                service_manifest=CapabilityManifest("weaviate", "vector", extra="weaviate"),
                target_identity=_target_identity,
                requires_deployment_id=True,
                locality="cluster",
            ),
        )
    if "weaviate" not in LEXICAL_INDEXES:
        LEXICAL_INDEXES.register(
            "weaviate",
            ProviderDescriptor(
                ProviderRef("lexical", "weaviate"),
                WeaviateSettings,
                frozenset({"lexical", "namespace_filter", "erase"}),
                requirement,
                WeaviateLexicalBuilder(),
                service_manifest=CapabilityManifest("weaviate", "lexical", extra="weaviate"),
                target_identity=_target_identity,
                requires_deployment_id=True,
                locality="cluster",
            ),
        )
