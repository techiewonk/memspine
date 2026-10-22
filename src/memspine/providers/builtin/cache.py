"""KV factories; remote connections stay behind their native client."""

from __future__ import annotations

import asyncio
from hashlib import sha256
from pathlib import Path

from memspine.config.schema.providers import (
    DiskCacheSettings,
    MemoryCacheSettings,
    RemoteCacheSettings,
)
from memspine.providers.contexts import CacheBuildContext
from memspine.providers.contracts import BuildResult, DependencyRequirement, ProviderRef
from memspine.providers.registry import CACHE_BACKENDS, ProviderDescriptor
from memspine.services.base import CapabilityManifest
from memspine.services.cache.base import KVCache, MemoryKV


def _owned_namespace(namespace: str, ctx: CacheBuildContext) -> str:
    """Keep shared cache entries inside one durable deployment's key scope."""
    owner = sha256(ctx.shared.owner_id.encode("utf-8")).hexdigest()
    return f"{namespace}:deployment:{owner}"


class MemoryBuilder:
    async def build(
        self, settings: MemoryCacheSettings, ctx: CacheBuildContext
    ) -> BuildResult[KVCache]:
        cache = MemoryKV(max_entries=settings.max_entries)

        async def probe() -> bool:
            await cache.get("__memspine_health_probe__")
            return True

        return BuildResult(
            cache,
            ProviderRef("cache", "memory"),
            (),
            frozenset({"kv", "ttl"}),
            health_probe=probe,
        )


class DiskBuilder:
    async def build(
        self, settings: DiskCacheSettings, ctx: CacheBuildContext
    ) -> BuildResult[KVCache]:
        from memspine.clients.cashews import CashewsClient, CashewsSpec
        from memspine.services.cache.cashews import CashewsCache

        # ADR-081: an unset path defaults under deployment.data_dir when one is configured.
        directory = (
            str(Path(ctx.data_dir) / "memspine.cache")
            if ctx.data_dir is not None and "path" not in settings.model_fields_set
            else settings.path
        )
        client = CashewsClient(CashewsSpec(f"disk://?directory={directory}"))
        ref = ctx.shared.resources.own("cache", client.close)
        await client.open()
        cache = CashewsCache(
            client,
            namespace=_owned_namespace(settings.namespace, ctx),
            default_ttl_seconds=settings.default_ttl_seconds,
        )

        async def probe() -> bool:
            await cache.get("__memspine_health_probe__")
            return True

        return BuildResult(
            cache,
            ProviderRef("cache", "disk"),
            (ref,),
            frozenset({"kv", "ttl"}),
            health_probe=probe,
        )


class RemoteBuilder:
    def __init__(self, name: str) -> None:
        self.name = name

    async def build(
        self, settings: RemoteCacheSettings, ctx: CacheBuildContext
    ) -> BuildResult[KVCache]:
        from memspine.clients.cashews import CashewsClient, CashewsSpec
        from memspine.services.cache.cashews import CashewsCache

        client = CashewsClient(CashewsSpec(settings.url))
        ref = ctx.shared.resources.own("cache", client.close)
        async with asyncio.timeout(settings.connect_timeout_s):
            await client.open()
        # Remote operations use the explicitly configured request deadline.
        cache = CashewsCache(
            client,
            namespace=_owned_namespace(settings.namespace, ctx),
            default_ttl_seconds=settings.default_ttl_seconds,
            request_timeout_s=settings.request_timeout_s,
        )

        async def probe() -> bool:
            await cache.get("__memspine_health_probe__")
            return True

        return BuildResult(
            cache,
            ProviderRef("cache", self.name),
            (ref,),
            frozenset({"kv", "ttl"}),
            health_probe=probe,
        )


def register() -> None:
    for name, model, builder, requirements in (
        ("memory", MemoryCacheSettings, MemoryBuilder(), ()),
        ("disk", DiskCacheSettings, DiskBuilder(), (DependencyRequirement("package", "cashews"),)),
        (
            "redis",
            RemoteCacheSettings,
            RemoteBuilder("redis"),
            (DependencyRequirement("package", "cashews"),),
        ),
        (
            "valkey",
            RemoteCacheSettings,
            RemoteBuilder("valkey"),
            (DependencyRequirement("package", "cashews"),),
        ),
    ):
        if name not in CACHE_BACKENDS:
            CACHE_BACKENDS.register(
                name,
                ProviderDescriptor(
                    ProviderRef("cache", name),
                    model,
                    frozenset({"kv", "ttl"}),
                    requirements,
                    builder,
                    service_manifest=(
                        CapabilityManifest(name, "cache", extra="cache")
                        if name != "memory"
                        else None
                    ),
                    requires_deployment_id=name in {"redis", "valkey"},
                    locality=(
                        "process" if name == "memory" else "host" if name == "disk" else "cluster"
                    ),
                ),
            )
