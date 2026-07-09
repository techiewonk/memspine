"""LMDB KVCache adapter (Phase 2): persistence, lazy TTL, namespace isolation."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

pytest.importorskip("lmdb")

from memspine.clients.lmdb import LmdbClient
from memspine.services.cache.lmdb_cache import LmdbCache


async def _open(path: Path, **kwargs: object) -> tuple[LmdbClient, LmdbCache]:
    client = LmdbClient(path)
    await client.connect()
    return client, LmdbCache(client, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
async def cache(tmp_path: Path) -> AsyncIterator[tuple[LmdbClient, LmdbCache]]:
    client, kv = await _open(tmp_path / "c.lmdb")
    yield client, kv
    await client.close()


async def test_round_trip_and_delete(cache: tuple[LmdbClient, LmdbCache]) -> None:
    _, kv = cache
    assert await kv.get("k") is None
    await kv.set("k", b"value")
    assert await kv.get("k") == b"value"
    await kv.delete("k")
    assert await kv.get("k") is None


async def test_ttl_zero_expires_immediately(cache: tuple[LmdbClient, LmdbCache]) -> None:
    _, kv = cache
    await kv.set("k", b"v", ttl_seconds=0.0)  # expires_at == now => already stale
    assert await kv.get("k") is None


async def test_no_ttl_persists(cache: tuple[LmdbClient, LmdbCache]) -> None:
    _, kv = cache
    await kv.set("k", b"v")  # expires_at 0.0 sentinel => never expires
    assert await kv.get("k") == b"v"


async def test_namespace_isolates_keys(tmp_path: Path) -> None:
    client, _ = await _open(tmp_path / "c.lmdb")
    try:
        a = LmdbCache(client, namespace="a")
        b = LmdbCache(client, namespace="b")
        await a.set("k", b"from-a")
        assert await b.get("k") is None  # same env, different prefix
        await b.set("k", b"from-b")
        assert await a.get("k") == b"from-a"
    finally:
        await client.close()


async def test_survives_reopen(tmp_path: Path) -> None:
    path = tmp_path / "c.lmdb"
    client, kv = await _open(path)
    await kv.set("k", b"durable")
    await client.close()

    client2, kv2 = await _open(path)
    try:
        assert await kv2.get("k") == b"durable"  # persisted to disk
    finally:
        await client2.close()
