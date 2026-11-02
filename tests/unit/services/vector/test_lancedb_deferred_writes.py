"""RM-160: LanceDB rows held by a caller's ``defer_writes`` and written in one table write."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.clients.lancedb import LanceDBClient, LanceDBSpec
from memspine.services.vector.lancedb_store import LanceDBVectorStore
from memspine.services.vector.per_namespace import PerNamespaceVectorStore


class _Embedder:
    embedder_id = "test"
    dim = 3


def _vec(i: int) -> list[float]:
    return [1.0, float(i), 0.5]


async def _store(exclusive: bool) -> tuple[LanceDBClient, LanceDBVectorStore]:
    client = LanceDBClient(LanceDBSpec("memory://vectors"))
    await client.open()
    return client, LanceDBVectorStore(client, _Embedder(), exclusive=exclusive)  # type: ignore[arg-type]


def _count_writes(monkeypatch: pytest.MonkeyPatch, table: Any) -> list[str]:
    calls: list[str] = []
    cls = type(table)
    add, merge = cls.add, cls.merge_insert

    def counted_add(this: Any, data: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(f"add:{len(data)}")
        return add(this, data, *args, **kwargs)

    def counted_merge(this: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append("merge")
        return merge(this, *args, **kwargs)

    monkeypatch.setattr(cls, "add", counted_add)
    monkeypatch.setattr(cls, "merge_insert", counted_merge)
    return calls


@pytest.mark.parametrize("exclusive", [True, False])
async def test_held_rows_are_written_in_one_call_and_reads_see_them(
    monkeypatch: pytest.MonkeyPatch, exclusive: bool
) -> None:
    client, store = await _store(exclusive)
    try:
        await store.upsert("warm", "n", "test", _vec(0))
        calls = _count_writes(monkeypatch, store._table)
        store.defer_writes()
        for i in range(1, 6):
            await store.upsert(f"r{i}", "n", "test", _vec(i))
        assert calls == [] and store.held_rows == 5
        hits = await store.query("n", _vec(3), "test", top_k=10)  # a read writes them first
        assert {h.record_id for h in hits} == {"warm", "r1", "r2", "r3", "r4", "r5"}
        assert calls == (["add:5"] if exclusive else ["merge"])
        await store.upsert("r6", "n", "test", _vec(6))
        await store.flush()
        assert store.held_rows == 0 and await store.exists("r6")
        await store.upsert("r7", "n", "test", _vec(7))  # the hold ended: written at once
        assert store.held_rows == 0
    finally:
        await client.close()


async def test_a_re_upsert_replaces_the_held_row_and_a_delete_drops_it() -> None:
    client, store = await _store(True)
    try:
        await store.upsert("warm", "n", "test", _vec(0))
        store.defer_writes()
        await store.upsert("a", "n", "test", _vec(1))
        await store.upsert("a", "n", "test", _vec(9))
        await store.upsert("b", "n", "test", _vec(2))
        await store.delete("b")  # never reaches the table
        assert store.held_rows == 1
        await store.flush()
        rows = store._table.to_arrow().to_pylist()
        assert sorted(r["record_id"] for r in rows) == ["a", "warm"]
        assert next(r for r in rows if r["record_id"] == "a")["vector"][1] == 9.0
        assert await store.history_absent("b") is False  # unpurged until the purge runs
        assert await store.purge_deleted()
        assert await store.history_absent("b") is True
    finally:
        await client.close()


async def test_the_hold_belongs_to_the_caller_and_nests() -> None:
    client, store = await _store(True)
    try:
        await store.upsert("warm", "n", "test", _vec(0))
        entered, release = asyncio.Event(), asyncio.Event()

        async def batch() -> None:
            store.defer_writes()
            await store.upsert("held", "n", "test", _vec(1))
            entered.set()
            await release.wait()
            await store.flush()

        task = asyncio.create_task(batch())
        await entered.wait()
        assert store.held_rows == 1
        # Not batching: written before it returns, with the other caller's held row.
        await store.upsert("free", "n", "test", _vec(2))
        assert store.held_rows == 0
        release.set()
        await task
        store.defer_writes()
        store.defer_writes()
        await store.upsert("x", "n", "test", _vec(3))
        await store.flush()  # inner: writes, the outer hold continues
        await store.upsert("y", "n", "test", _vec(4))
        assert store.held_rows == 1
        await store.flush()
        await store.upsert("z", "n", "test", _vec(5))
        assert store.held_rows == 0
    finally:
        await client.close()


async def test_per_namespace_tables_share_one_hold(tmp_path: Path) -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {"provider": "sqlite", "settings": {"path": str(tmp_path / "m.db")}},
            "vector": {"provider": "lancedb", "settings": {"isolation": "per_namespace"}},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        read={"record_access": False},
    )
    await eng.start()
    try:
        vector = eng._vector
        assert isinstance(vector, PerNamespaceVectorStore)
        async with eng._projection_batch():
            a = await eng.write("camping by the lake", namespace="alice")
            b = await eng.write("skiing in the alps", namespace="bob")  # a table opened mid-batch
            held = sum(s.held_rows for s in vector._stores.values())
            assert held >= 1
        assert sum(s.held_rows for s in vector._stores.values()) == 0
        assert await vector.exists(a.record_id) and await vector.exists(b.record_id)
    finally:
        await eng.stop()
