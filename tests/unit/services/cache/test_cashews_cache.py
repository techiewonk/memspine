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


async def test_compact_leaves_no_deleted_key_in_the_disk_files(tmp_path: Path) -> None:
    """ADR-095: a delete is logical in diskcache's SQLite; ``compact`` rewrites the database and
    truncates its WAL so the deleted key is in no file page. Live keys survive."""
    directory = tmp_path / "cache"
    client = CashewsClient(CashewsSpec(f"disk://?directory={directory}"))
    await client.open()
    try:
        kv = CashewsCache(client)
        assert await kv.compact() is True  # nothing deleted yet: a no-op
        await kv.set("emb:erased-3f9a1c", b"x" * 64)
        await kv.set("emb:kept", b"y")
        await kv.delete("emb:erased-3f9a1c")
        assert await kv.compact() is True
        assert await kv.get("emb:kept") == b"y"
        hits = [
            p for p in directory.rglob("*") if p.is_file() and b"erased-3f9a1c" in p.read_bytes()
        ]
        assert hits == []
    finally:
        await client.close()


@pytest.mark.parametrize("disk", [True, False])
async def test_delete_prefix(tmp_path: Path, disk: bool) -> None:
    """ADR-095: every key of this cache's namespace with the prefix goes (the sharded disk
    backend is walked directly; cashews' own pattern scan skips it)."""
    url = f"disk://?directory={tmp_path / 'cache'}" if disk else "mem://"
    client = CashewsClient(CashewsSpec(url))
    await client.open()
    try:
        kv = CashewsCache(client, namespace="a")
        other = CashewsCache(client, namespace="b")
        for key in ("embq:1", "embqV:2", "emb:3"):
            await kv.set(key, b"v")
        await other.set("embq:1", b"w")
        await kv.delete_prefix("embq")
        assert await kv.get("embq:1") is None and await kv.get("embqV:2") is None
        assert await kv.get("emb:3") == b"v"
        assert await other.get("embq:1") == b"w"  # another namespace keeps its keys
    finally:
        await client.close()


async def test_compact_is_a_no_op_on_a_memory_backend() -> None:
    client = CashewsClient(CashewsSpec("mem://"))
    await client.open()
    try:
        kv = CashewsCache(client)
        await kv.set("k", b"v")
        await kv.delete("k")
        assert await kv.compact() is True
    finally:
        await client.close()
