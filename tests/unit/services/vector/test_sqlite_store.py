"""SQLite vector fallback: upsert/query/delete + namespace isolation."""

from __future__ import annotations

from memspine.clients.sqlite import SQLiteClient
from memspine.services.storage.sqlite.schema import metadata
from memspine.services.vector.sqlite_store import SQLiteVectorStore


async def make_store() -> SQLiteVectorStore:
    client = SQLiteClient(":memory:")
    await client.connect()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    return SQLiteVectorStore(client)


async def test_query_ranks_by_cosine_and_respects_namespace() -> None:
    store = await make_store()
    await store.upsert("r1", "ns/a", "hash:4", [1.0, 0.0, 0.0, 0.0])
    await store.upsert("r2", "ns/a", "hash:4", [0.7, 0.7, 0.0, 0.0])
    await store.upsert("r3", "ns/b", "hash:4", [1.0, 0.0, 0.0, 0.0])

    hits = await store.query("ns/a", [1.0, 0.0, 0.0, 0.0], top_k=5)
    assert [hit.record_id for hit in hits] == ["r1", "r2"]  # r3 is another namespace
    assert hits[0].score > hits[1].score
    assert abs(hits[0].score - 1.0) < 1e-6


async def test_upsert_replaces_and_delete_removes() -> None:
    store = await make_store()
    await store.upsert("r1", "ns/a", "hash:4", [1.0, 0.0, 0.0, 0.0])
    await store.upsert("r1", "ns/a", "hash:4", [0.0, 1.0, 0.0, 0.0])  # replace
    hits = await store.query("ns/a", [0.0, 1.0, 0.0, 0.0])
    assert len(hits) == 1 and abs(hits[0].score - 1.0) < 1e-6

    await store.delete("r1")
    assert await store.query("ns/a", [0.0, 1.0, 0.0, 0.0]) == []

    await store.upsert("r2", "ns/a", "hash:4", [1.0, 0.0, 0.0, 0.0])
    await store.delete_all()
    assert await store.query("ns/a", [1.0, 0.0, 0.0, 0.0]) == []
