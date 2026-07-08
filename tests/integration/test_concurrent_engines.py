"""Two file-backed Engines on ONE db file — write serialization under real file locking.

Per-namespace ``asyncio.Lock``s only serialize writers *inside one Engine
instance*. Two Engine instances over the same database file share no such lock —
the only thing standing between them is SQLite's own file locking (WAL +
``busy_timeout``) and the ``memory_events`` autoincrement primary key that mints
``seq``. Whether that is enough to prevent lost updates / seq collisions is a
property of the real on-disk engine, invisible to a single-instance ``:memory:``
unit test.

REGRESSION GUARDED: a second concurrent writer causing a lost update, a duplicated
/ non-monotonic ``seq``, or a corrupted read model on the shared file.

DISCOVERED CONTRACT (asserted below): two Engines on one file interleave cleanly.
Every concurrent write lands as its own event with a distinct, gap-free monotonic
``seq``; the final event count equals the total writes; and a rebuild reproduces
all of them. No lost updates, no corruption, no ``SQLITE_BUSY`` surfaced (the 5s
busy timeout absorbs contention). Had this instead shown data corruption or lost
updates, that would be a real bug to report — it does not.
"""

from __future__ import annotations

import asyncio
from typing import Any

#: Writes per engine. Enough concurrent contention on the shared file to expose a
#: locking/seq defect, small enough to stay fast.
_PER_ENGINE = 25


async def test_two_engines_one_file_no_lost_updates(make_file_engine: Any) -> None:
    """Concurrent writes through two Engines on the same file all persist with
    distinct monotonic seqs; the read model and a rebuild account for every one."""
    engine_a = make_file_engine()
    engine_b = make_file_engine()
    await engine_a.start()
    await engine_b.start()
    try:

        async def write_batch(engine: Any, tag: str) -> list[str]:
            ids: list[str] = []
            for i in range(_PER_ENGINE):
                record = await engine.write(f"{tag} content {i}", namespace="default")
                ids.append(record.record_id)
            return ids

        # Drive BOTH engines' write loops concurrently against the same namespace.
        ids_a, ids_b = await asyncio.gather(
            write_batch(engine_a, "A"),
            write_batch(engine_b, "B"),
        )

        total = 2 * _PER_ENGINE
        # No lost write, no colliding record id.
        distinct_ids = set(ids_a) | set(ids_b)
        assert len(distinct_ids) == total

        storage = engine_a._storage
        assert storage is not None
        events = await storage.read_events(after_seq=0, limit=10_000)
        seqs = [event.seq for event in events]

        # Every append got a distinct, gap-free, monotonically increasing seq.
        assert len(events) == total, "an event was lost or duplicated under concurrency"
        assert seqs == list(range(1, total + 1)), "seqs are not contiguous/monotonic"

        # The read model materialized exactly the total set of writes.
        records = await storage.list_records("default")
        assert len(records) == total
        assert {r.record_id for r in records} == distinct_ids
    finally:
        await engine_a.stop()
        await engine_b.stop()

    # ── a fresh engine rebuilds the whole thing from the shared log ───────────
    reopened = make_file_engine()
    await reopened.start()
    try:
        counts = await reopened.rebuild()
        assert counts["records"] == 2 * _PER_ENGINE
        assert len(await reopened.retrieve(memory_type="semantic")) == 2 * _PER_ENGINE
    finally:
        await reopened.stop()
