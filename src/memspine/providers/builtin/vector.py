"""LanceDB construction and its namespace/versioned-space composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import cast

from memspine.config import constants
from memspine.config.schema.providers import LanceDBSettings
from memspine.exceptions import ConfigError
from memspine.models.encoders.embedding.base import EmbeddingService
from memspine.providers.contexts import VectorBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.placement import generation_component
from memspine.providers.registry import VECTOR_STORES, ProviderDescriptor
from memspine.services.vector.base import VectorStore


class LanceDBBuilder:
    async def build(
        self, settings: LanceDBSettings, ctx: VectorBuildContext
    ) -> BuildResult[VectorStore]:
        from memspine.clients.lancedb import LanceDBClient
        from memspine.services.vector.lancedb_store import LanceDBVectorStore
        from memspine.services.vector.per_namespace import PerNamespaceVectorStore
        from memspine.store.vector_spaces import JsonSpaceState, VectorSpace, VersionedSpaceStore

        placement = ctx.placement
        if placement.in_memory and ctx.generation_id != "active":
            raise ConfigError("controlled vector generations require durable storage placement")
        scoped_generation = generation_component(ctx.generation_id)
        generation_suffix = (
            "" if ctx.generation_id == "active" else f".generation-{scoped_generation}"
        )
        path = (
            "memory://vectors"
            if placement.in_memory
            else f"{placement.base_path}.lance{generation_suffix}"
        )
        client = LanceDBClient(path)
        ref = ctx.shared.resources.own("vector", client.close)
        await client.connect()

        def make(suffix: str = "", embedding: EmbeddingService | None = None) -> LanceDBVectorStore:
            return LanceDBVectorStore(
                client,
                embedding if embedding is not None else ctx.embedding,
                quantization=ctx.quantization,
                matryoshka_dim=ctx.matryoshka_dim,
                oversample=constants.RESCORE_OVERSAMPLE,
                compact_every=constants.LANCE_COMPACT_EVERY if placement.in_memory else None,
                exclusive=placement.in_memory,
                namespace_index=settings.namespace_index,
                table_suffix=suffix,
                profile_meta=None if ctx.profile_meta is None else dict(ctx.profile_meta),
            )

        service: VectorStore
        if ctx.versioned_spaces:

            @dataclass(frozen=True)
            class SpaceEmbedding:
                embedder_id: str
                dim: int

            current = VectorSpace(
                model_id=ctx.embedding.embedder_id,
                dim=ctx.embedding.dim,
                query_instruction=ctx.query_instruction,
            )

            def factory(space: VectorSpace) -> VectorStore:
                # Native table creation only consumes identity and dimension.
                shim = cast(EmbeddingService, SpaceEmbedding(space.qualified_id, space.dim))
                if settings.isolation == "per_namespace":
                    return PerNamespaceVectorStore(client, shim, lambda suffix: make(suffix, shim))
                return make("", shim)

            state = (
                None
                if placement.in_memory
                else JsonSpaceState(f"{placement.base_path}.spaces{generation_suffix}.json")
            )
            service = cast(VectorStore, VersionedSpaceStore(factory, current, state))
        elif settings.isolation == "per_namespace":
            service = cast(VectorStore, PerNamespaceVectorStore(client, ctx.embedding, make))
        else:
            service = make()

        async def probe() -> bool:
            await service.exists("__memspine_health_probe__")
            return True

        return BuildResult(
            service,
            ProviderRef("vector", "lancedb"),
            (ref,),
            frozenset({"vector", "namespace_filter", "erase", "quantization", "versioned_spaces"}),
            health_probe=probe,
        )


def register() -> None:
    if "lancedb" not in VECTOR_STORES:
        VECTOR_STORES.register(
            "lancedb",
            ProviderDescriptor(
                ProviderRef("vector", "lancedb"),
                LanceDBSettings,
                frozenset(
                    {"vector", "namespace_filter", "erase", "quantization", "versioned_spaces"}
                ),
                (),
                LanceDBBuilder(),
            ),
        )
