"""Redis / Valkey KVCache adapter (D-09), ``[redis]`` / ``[valkey]``.

The shared, cross-process cache: native ``EX`` TTL (no lazy-expiry bookkeeping),
byte values round-tripped verbatim. Keys are prefixed with the configured
namespace so many memspine instances can share one server without colliding.
Takes the raw async Redis-protocol client (from :class:`RedisClient`) so tests
can inject a ``fakeredis`` client directly without a live server.
"""

from __future__ import annotations

import math
from typing import Any

__all__ = ["RedisCache"]


class RedisCache:
    def __init__(
        self,
        client: Any,
        *,
        namespace: str = "memspine",
        default_ttl_seconds: float | None = None,
    ) -> None:
        self._redis = client
        self._prefix = f"{namespace}:"
        self._default_ttl = default_ttl_seconds

    def _key(self, key: str) -> str:
        return self._prefix + key

    async def get(self, key: str) -> bytes | None:
        value: bytes | None = await self._redis.get(self._key(key))
        return value

    async def set(self, key: str, value: bytes, ttl_seconds: float | None = None) -> None:
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        if ttl is not None:
            # Redis EX takes whole seconds; round up so a sub-second TTL still
            # gets at least one second rather than silently becoming "no expiry".
            await self._redis.set(self._key(key), value, ex=max(1, math.ceil(ttl)))
        else:
            await self._redis.set(self._key(key), value)

    async def delete(self, key: str) -> None:
        await self._redis.delete(self._key(key))
