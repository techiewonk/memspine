"""#88: two SQLite clients on one fresh file, no "database is locked".

Root cause of the flaky ``test_two_writers_one_file_no_lost_updates``: the Alembic
migration created the file in rollback mode and the switch to WAL waited for the
first pooled connection. Two engines' first write connections then both asked for
the switch at once; it needs an exclusive lock and does not wait on the busy
timeout, so one failed within a millisecond. The client now switches the file at
``connect()`` (retrying), and later connections only check the mode.
"""

from __future__ import annotations

import asyncio
import sqlite3

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.services.storage.sqlite.engine import SQLiteStorage

_PER_WRITER = 25


def _journal_mode(path: str) -> str:
    conn = sqlite3.connect(path)
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


async def _open(path: str) -> tuple[SQLiteClient, SQLiteStorage]:
    client = SQLiteClient(path)
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    return client, storage


async def _write(storage: SQLiteStorage, tag: str) -> None:
    for i in range(_PER_WRITER):
        record = MemoryRecord(namespace="default", memory_type="semantic", content=f"{tag} {i}")
        await storage.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="default",
                actor="user",
                payload={"record": record.model_dump(mode="json")},
            )
        )


async def test_file_is_wal_from_connect_before_any_migration(db_path: str) -> None:
    client = SQLiteClient(db_path)
    await client.connect()
    try:
        assert _journal_mode(db_path) == "wal"
    finally:
        await client.close()


async def test_file_stays_wal_after_migration(db_path: str) -> None:
    client, _storage = await _open(db_path)
    try:
        assert _journal_mode(db_path) == "wal"
    finally:
        await client.close()


async def test_two_clients_started_together_write_without_locking(db_path: str) -> None:
    """Concurrent start (two migrations in one process) and concurrent appends, on
    several fresh files: no lock error, no lost or duplicated event."""
    for round_ in range(5):
        path = f"{db_path}.{round_}"
        (client_a, storage_a), (client_b, storage_b) = await asyncio.gather(
            _open(path), _open(path)
        )
        try:
            await asyncio.gather(_write(storage_a, "A"), _write(storage_b, "B"))
            events = await storage_a.read_events(after_seq=0, limit=10_000)
            assert [e.seq for e in events] == list(range(1, 2 * _PER_WRITER + 1))
        finally:
            await client_a.close()
            await client_b.close()
