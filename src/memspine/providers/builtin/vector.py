"""LanceDB construction and its namespace/versioned-space composition."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from memspine.config import constants
from memspine.config.schema.providers import LanceDBSettings
from memspine.exceptions import ConfigError
from memspine.providers.contexts import VectorBuildContext
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.placement import generation_component, host_base_path
from memspine.providers.registry import VECTOR_STORES, ProviderDescriptor
from memspine.providers.shared import own_shared, path_identity, resource_key
from memspine.services.embedding.base import EmbeddingService
from memspine.services.vector.base import VectorStore

if TYPE_CHECKING:
    from memspine.clients.lancedb import LanceDBClient


class LanceDBBuilder:
    async def build(
        self, settings: LanceDBSettings, ctx: VectorBuildContext
    ) -> BuildResult[VectorStore]:
        from memspine.clients.lancedb import LanceDBClient, LanceDBSpec
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
            else f"{host_base_path(placement, 'vector', 'lancedb')}.lance{generation_suffix}"
        )
        client: LanceDBClient
        if placement.in_memory:
            # An in-memory database is private to this Engine, like its ``:memory:`` event log.
            client = LanceDBClient(LanceDBSpec(path))
            ref = ctx.shared.resources.own("vector", client.close)
            await client.open()
        else:

            async def open_client() -> LanceDBClient:
                opened = LanceDBClient(LanceDBSpec(path))
                await opened.open()
                return opened

            async def close_client(opened: LanceDBClient) -> None:
                await opened.close()

            # RM-77 (ADR-079): one connection per database path per process, so the stores of
            # every Engine on the path share its write epochs and read each other's commits.
            client, ref = await own_shared(
                ctx.shared.resources,
                "vector",
                resource_key("vector.lancedb", {"path": path_identity(path)}, scope="thread"),
                open_client,
                close=close_client,
                registry=ctx.shared.resource_registry(),
            )

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
                else JsonSpaceState(
                    f"{host_base_path(placement, 'vector', 'lancedb')}.spaces{generation_suffix}.json"
                )
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
                # PA-046: `import lancedb` is ~1.2 s, mostly its eager lance_namespace
                # REST-model import; prewarming overlaps it with the rest of startup.
                prewarm_modules=("lancedb",),
                locality="host",
            ),
        )
