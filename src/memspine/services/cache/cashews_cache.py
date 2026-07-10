"""cashews KVCache adapter (D-09, ``[cache]``): the unified on-disk + redis/valkey
cache (ADR-022 amendment — replaces the LMDB and hand-rolled Redis adapters).

Wraps a set-up ``cashews.Cache`` (owned by :class:`CashewsClient`): keys are
namespace-prefixed so many memspine instances can share one backend, TTL rides
cashews' native ``expire`` (no lazy-expiry bookkeeping), and byte values
round-trip verbatim. ``MemoryKV`` stays the zero-dep core ``memory`` default
(slim-core, D-03); cashews only backs the ``disk``/``redis``/``valkey`` backends.
"""

from __future__ import annotations

from memspine.clients.cashews import CashewsClient

__all__ = ["CashewsCache"]


class CashewsCache:
    def __init__(
        self,
        client: CashewsClient,
        *,
        namespace: str = "memspine",
        default_ttl_seconds: float | None = None,
    ) -> None:
        self._client = client
        self._prefix = f"{namespace}:"
        self._default_ttl = default_ttl_seconds

    def _key(self, key: str) -> str:
        return self._prefix + key

    async def get(self, key: str) -> bytes | None:
        value: bytes | None = await self._client.cache.get(self._key(key))
        return value

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        # cashews ``expire=None`` means no expiry; a number is seconds.
        await self._client.cache.set(self._key(key), value, expire=ttl)

    async def delete(self, key: str) -> None:
        await self._client.cache.delete(self._key(key))
