"""Weaviate vector and lexical provider descriptors."""

from __future__ import annotations

import hashlib
import os
from typing import TYPE_CHECKING, cast

from pydantic import BaseModel, JsonValue

from memspine.config.remote import endpoint_from_env
from memspine.config.schema.remote_providers import WeaviateSettings
from memspine.exceptions import ConfigError
from memspine.providers.contexts import LexicalBuildContext, VectorBuildContext
from memspine.providers.contracts import (
    BuildResult,
    DependencyRequirement,
    LifecycleManager,
    ProviderRef,
    ResourceRef,
)
from memspine.providers.registry import LEXICAL_INDEXES, VECTOR_STORES, ProviderDescriptor
from memspine.providers.shared import own_shared, resource_key
from memspine.services.base import CapabilityManifest
from memspine.services.lexical.base import LexicalStore
from memspine.services.vector.base import VectorStore
from memspine.services.weaviate_projection import collection_name, ensure_collection

if TYPE_CHECKING:
    from memspine.clients.weaviate import WeaviateClient


def _target_identity(model: BaseModel) -> dict[str, JsonValue]:
    if not isinstance(model, WeaviateSettings):
        raise ConfigError("Weaviate target identity received incompatible settings")
    host, scheme, port, _ = endpoint_from_env(model.url_env, allowed_schemes=("http", "https"))
    grpc_host, grpc_scheme, grpc_port = host, scheme, model.grpc_port
    if model.grpc_url_env is not None:
        grpc_host, grpc_scheme, grpc_port, _ = endpoint_from_env(
            model.grpc_url_env, allowed_schemes=("http", "https")
        )
    token = os.environ.get(model.api_key_env, "") if model.api_key_env else ""
    if model.api_key_env and not token:
        raise ConfigError(f"Weaviate authentication needs environment variable {model.api_key_env}")
    return {
        "kind": "weaviate",
        "host": host.lower(),
        "scheme": scheme,
        "port": port,
        "grpc_host": grpc_host.lower(),
        "grpc_scheme": grpc_scheme,
        "grpc_port": grpc_port,
        "auth_digest": hashlib.sha256(token.encode()).hexdigest(),
        "canonical_target": {
            "kind": "weaviate",
            "host": host.lower(),
            "port": port,
            "grpc_host": grpc_host.lower(),
            "grpc_port": grpc_port,
        },
    }


async def _shared_client(
    settings: WeaviateSettings, resources: LifecycleManager, resource_id: str
) -> tuple[WeaviateClient, ResourceRef]:
    """One connected client per Weaviate target and transport settings (ADR-079).

    The vector and lexical services of one Engine (and Engines in this process on the same
    loop) share it; each service still ensures its own collection. The key holds only the
    credential-free target identity and an auth digest."""
    from memspine.clients.weaviate import WeaviateClient

    knobs = settings.model_dump(mode="json", exclude={"collection_prefix"})

    async def open_client() -> WeaviateClient:
        client = WeaviateClient(settings)
        await client.connect()
        return client

    async def close_client(client: WeaviateClient) -> None:
        await client.close()

    return await own_shared(
        resources,
        resource_id,
        resource_key("weaviate.client", _target_identity(settings), knobs, scope="loop"),
        open_client,
        close=close_client,
    )


class WeaviateVectorBuilder:
    async def build(
        self, settings: WeaviateSettings, ctx: VectorBuildContext
    ) -> BuildResult[VectorStore]:
        from memspine.services.vector.weaviate_store import WeaviateVectorStore

        if not ctx.shared.owner_id:
            raise ConfigError("Weaviate requires a stable runtime.deployment_id")
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
        client, ref = await _shared_client(settings, ctx.shared.resources, "vector")
        collection = await ensure_collection(client.native, name, include_content=False)
        service = WeaviateVectorStore(
            collection,
            owner_id=ctx.shared.owner_id,
            embedder_id=ctx.embedding.embedder_id,
            dimension=ctx.embedding.dim,
        )

        async def probe() -> bool:
            return bool(
                await client.health()
                and await client.native.collections.exists(name)
                and await service.exists("__memspine_health_probe__") is False
            )

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
            raise ConfigError("Weaviate requires a stable runtime.deployment_id")
        name = collection_name(
            settings.collection_prefix, "Lexical", ctx.shared.owner_id, ctx.generation_id
        )
        client, ref = await _shared_client(settings, ctx.shared.resources, "lexical")
        collection = await ensure_collection(client.native, name, include_content=True)
        service = WeaviateLexicalStore(collection, owner_id=ctx.shared.owner_id)

        async def probe() -> bool:
            return bool(
                await client.health()
                and await client.native.collections.exists(name)
                and await service.exists("__memspine_health_probe__") is False
            )

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
            ),
        )
