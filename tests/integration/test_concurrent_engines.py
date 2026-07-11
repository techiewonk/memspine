"""Two writers on ONE db file — event-log write serialization under real file locking.

Only the **append-only event log** (SQLite, WAL + ``busy_timeout``) supports
concurrent writers on one file: its autoincrement primary key mints a distinct,
gap-free ``seq`` for every append no matter who wrote it. The *derived* stores —
LanceDB vectors, the Tantivy lexical index — are single-writer embedded indexes
and cannot be shared by two live Engine instances at once (that is what the
server ``opensearch`` lexical provider and a shared vector service are for). So
this test drives concurrency at the layer that actually supports it: two
``SQLiteStorage`` instances over the same file append events concurrently, and a
single fresh Engine then rebuilds every record from the shared log.

REGRESSION GUARDED: a second concurrent writer causing a lost update, a
duplicated / non-monotonic ``seq``, or a log a rebuild cannot fully replay.

DISCOVERED CONTRACT (asserted below): concurrent appends interleave cleanly —
every write lands as its own event with a distinct, gap-free monotonic ``seq``,
the final event count equals the total writes, and a rebuild reproduces all of
them. No lost updates, no corruption, no ``SQLITE_BUSY`` surfaced (the 5s busy
timeout absorbs contention).
"""

from __future__ import annotations

import asyncio
from typing import Any

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.services.storage.sqlite.engine import SQLiteStorage

#: Writes per writer. Enough concurrent contention on the shared file to expose a
#: locking/seq defect, small enough to stay fast.
_PER_WRITER = 25


async def _storage(db_path: str) -> tuple[SQLiteClient, SQLiteStorage]:
    client = SQLiteClient(db_path)
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()  # create_all is idempotent, so a second opener is safe
    return client, storage


async def test_two_writers_one_file_no_lost_updates(db_path: str, make_file_engine: Any) -> None:
    client_a, storage_a = await _storage(db_path)
    client_b, storage_b = await _storage(db_path)
    try:

        async def write_batch(storage: SQLiteStorage, tag: str) -> list[str]:
            ids: list[str] = []
            for i in range(_PER_WRITER):
                record = MemoryRecord(
                    namespace="default", memory_type="semantic", content=f"{tag} content {i}"
                )
                await storage.append_event(
                    MemoryEvent(
                        kind=EventKind.WRITE,
                        namespace="default",
                        actor="user",
                        payload={"record": record.model_dump(mode="json")},
                    )
                )
                ids.append(record.record_id)
            return ids

        # Drive BOTH writers concurrently against the same file.
        ids_a, ids_b = await asyncio.gather(
            write_batch(storage_a, "A"), write_batch(storage_b, "B")
        )

        total = 2 * _PER_WRITER
        # No lost write, no colliding record id.
        distinct_ids = set(ids_a) | set(ids_b)
        assert len(distinct_ids) == total

        events = await storage_a.read_events(after_seq=0, limit=10_000)
        seqs = [event.seq for event in events]
        # Every append got a distinct, gap-free, monotonically increasing seq.
        assert len(events) == total, "an event was lost or duplicated under concurrency"
        assert seqs == list(range(1, total + 1)), "seqs are not contiguous/monotonic"
    finally:
        await client_a.close()
        await client_b.close()

    # ── a fresh single Engine rebuilds the whole thing from the shared log ─────
    # hybrid off: this step verifies the RECORD projection rebuilds from the log,
    # not the lexical index (retrieve() reads the record store, not BM25).
    reopened = make_file_engine(read={"hybrid": False})
    await reopened.start()
    try:
        counts = await reopened.rebuild()
        assert counts["records"] == total
        assert len(await reopened.retrieve(memory_type="semantic")) == total
    finally:
        await reopened.stop()
