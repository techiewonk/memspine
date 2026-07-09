"""Redis/Valkey KVCache adapter (Phase 2), exercised against fakeredis."""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fakeredis")

from memspine.services.cache.redis_cache import RedisCache


def _fake() -> Any:
    from fakeredis import aioredis

    return aioredis.FakeRedis()


async def test_round_trip_and_delete() -> None:
    kv = RedisCache(_fake())
    assert await kv.get("k") is None
    await kv.set("k", b"value")
    assert await kv.get("k") == b"value"
    await kv.delete("k")
    assert await kv.get("k") is None


async def test_namespace_prefixes_keys() -> None:
    client = _fake()
    a = RedisCache(client, namespace="a")
    b = RedisCache(client, namespace="b")
    await a.set("k", b"from-a")
    assert await b.get("k") is None
    # the physical key is prefixed, not the bare "k"
    assert await client.get("a:k") == b"from-a"


async def test_ttl_sets_native_expiry() -> None:
    client = _fake()
    kv = RedisCache(client, namespace="ns")
    await kv.set("k", b"v", ttl_seconds=1.5)  # ceil -> 2s
    ttl = await client.ttl("ns:k")
    assert ttl > 0  # native EX expiry attached, not "no expiry"


async def test_default_ttl_applies_when_unspecified() -> None:
    client = _fake()
    kv = RedisCache(client, namespace="ns", default_ttl_seconds=30)
    await kv.set("k", b"v")  # no per-call ttl => default applies
    assert await client.ttl("ns:k") > 0
