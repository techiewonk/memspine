"""RM-161: inside ``sharing_listed_records`` the SQL store reuses the record it built for an
unchanged row, never for a changed one, and forgets a record that is written or deleted."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.services.storage.records_port import (
    shares_listed_records,
    sharing_listed_records,
)
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.storage.sqlite.schema import memory_records
from memspine.store.records import MemoryRecord

T0 = datetime(2026, 1, 1, tzinfo=UTC)


async def _storage() -> SQLiteStorage:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    return storage


def _record(record_id: str, content: str, memory_type: str = "episodic") -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace="a",
        memory_type=memory_type,
        content=content,
        valid_from=T0,
    )


async def test_unchanged_rows_are_shared_only_inside_the_scope() -> None:
    storage = await _storage()
    try:
        await storage.upsert_record(_record("r1", "one"))
        await storage.upsert_record(_record("r2", "two", "semantic"))
        outside = await storage.list_records("a")
        assert outside == await storage.list_records("a")
        assert outside[0] is not (await storage.list_records("a"))[0]  # fresh objects outside
        assert not shares_listed_records()
        with sharing_listed_records():
            assert shares_listed_records()
            first = {r.record_id: r for r in await storage.list_records("a")}
            again = {r.record_id: r for r in await storage.list_records("a")}
            typed = await storage.list_records("a", "semantic")
        assert not shares_listed_records()
        assert first == {r.record_id: r for r in outside}  # the same records as unshared
        assert again["r1"] is first["r1"] and again["r2"] is first["r2"]
        assert typed == [first["r2"]] and typed[0] is first["r2"]
    finally:
        await storage.stop()


async def test_a_changed_or_deleted_row_is_never_served_from_the_share() -> None:
    storage = await _storage()
    try:
        await storage.upsert_record(_record("r1", "one"))
        await storage.upsert_record(_record("r2", "two"))
        with sharing_listed_records():
            before = {r.record_id: r for r in await storage.list_records("a")}
            await storage.upsert_record(_record("r1", "one, edited"))
            await storage.delete_record("r2")
            after = {r.record_id: r for r in await storage.list_records("a")}
        assert set(after) == {"r1"}
        assert after["r1"].content == "one, edited" and after["r1"] is not before["r1"]
        assert "r2" not in storage._listed["a"]  # the deleted row is not kept in memory
        with sharing_listed_records():
            await storage.delete_all_records()
            assert storage._listed == {}
            assert await storage.list_records("a") == []
    finally:
        await storage.stop()


async def test_a_row_changed_by_another_writer_is_rebuilt() -> None:
    """The share is checked against the stored row itself, so a write this store object
    did not see (another Engine or process on the same database) is never hidden."""
    storage = await _storage()
    try:
        await storage.upsert_record(_record("r1", "one"))
        with sharing_listed_records():
            (first,) = await storage.list_records("a")
            # a direct UPDATE bypasses this object's invalidation, as a separate writer would
            async with storage._client.engine.begin() as conn:
                await conn.execute(
                    memory_records.update()
                    .where(memory_records.c.record_id == "r1")
                    .values(content="changed elsewhere")
                )
            (second,) = await storage.list_records("a")
        assert second.content == "changed elsewhere" and second is not first
    finally:
        await storage.stop()


async def test_a_listing_in_flight_during_a_hard_delete_never_memoises_the_erased_record() -> None:
    """ADR-095 (in-process copies): rows fetched before a hard delete may still reach the reader
    that fetched them, but the store's memo never keeps the erased record afterwards."""
    from sqlalchemy import select

    storage = await _storage()
    try:
        await storage.upsert_record(_record("gone", "secret passphrase"))
        await storage.upsert_record(_record("kept", "harmless"))
        async with storage._client.engine.connect() as conn:  # the in-flight listing's fetch
            rows = (
                await conn.execute(select(memory_records).where(memory_records.c.namespace == "a"))
            ).all()
        await storage.delete_record("gone")  # the hard erase lands while that read is in flight
        with sharing_listed_records():
            out = storage._shared_records("a", rows, whole=True)
        assert {r.record_id for r in out} == {"gone", "kept"}  # the reader sees its own fetch
        memo = storage._listed.get("a", {})
        assert "gone" not in memo and "kept" in memo
        assert "secret" not in repr(storage._listed)
    finally:
        await storage.stop()
