"""cashews KVCache adapter (A0, ADR-022 amendment): round-trip, namespace,
disk persistence — exercised on the in-memory + disk cashews backends."""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("cashews")

from memspine.clients.cashews import CashewsClient, CashewsSpec
from memspine.services.cache.cashews import CashewsCache


async def test_round_trip_and_delete() -> None:
    client = CashewsClient(CashewsSpec("mem://"))
    await client.open()
    try:
        kv = CashewsCache(client)
        assert await kv.get("k") is None
        await kv.set("k", b"value")
        assert await kv.get("k") == b"value"
        await kv.set("k2", b"other", ttl_seconds=60)  # TTL accepted
        assert await kv.get("k2") == b"other"
        await kv.delete("k")
        assert await kv.get("k") is None
    finally:
        await client.close()


async def test_namespace_isolates_keys() -> None:
    client = CashewsClient(CashewsSpec("mem://"))
    await client.open()
    try:
        a = CashewsCache(client, namespace="a")
        b = CashewsCache(client, namespace="b")
        await a.set("k", b"from-a")
        assert await b.get("k") is None  # same backend, different prefix
        await b.set("k", b"from-b")
        assert await a.get("k") == b"from-a"
    finally:
        await client.close()


async def test_disk_survives_reopen(tmp_path: Path) -> None:
    url = f"disk://?directory={tmp_path / 'cache'}"
    c1 = CashewsClient(CashewsSpec(url))
    await c1.open()
    await CashewsCache(c1).set("k", b"durable")
    await c1.close()

    c2 = CashewsClient(CashewsSpec(url))
    await c2.open()
    try:
        assert await CashewsCache(c2).get("k") == b"durable"  # persisted to disk
    finally:
        await c2.close()
