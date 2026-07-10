"""Storage-contract parity (Phase 6): the SAME assertions run against sqlite and
postgres, proving :class:`SqlStorage` is backend-agnostic (D-36, ADR-025).

Postgres is skip-if-unavailable: set ``MEMSPINE_TEST_POSTGRES_URL`` to a DSN
(e.g. ``postgresql://user:pass@localhost:5432/memspine_test``) to exercise it;
without it, only the sqlite parametrization runs. The postgres run drops and
recreates the shared schema for isolation.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from typing import Any

import pytest

from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.core.records import MemoryRecord, SourceInfo


def _ev(namespace: str, content: str) -> MemoryEvent:
    return MemoryEvent(kind=EventKind.WRITE, namespace=namespace, payload={"content": content})


def _rec(record_id: str, namespace: str, content: str, **extra: Any) -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id,
        namespace=namespace,
        memory_type="semantic",
        content=content,
        source=SourceInfo(role="user"),
        **extra,
    )


async def _sqlite() -> tuple[Any, Any]:
    from memspine.clients.sqlite import SQLiteClient
    from memspine.services.storage.sqlite.engine import SQLiteStorage

    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client, mode=EventLogMode.ROLLING)
    await storage.start()
    return client, storage


async def _postgres() -> tuple[Any, Any]:
    url = os.environ.get("MEMSPINE_TEST_POSTGRES_URL")
    if not url:
        pytest.skip("set MEMSPINE_TEST_POSTGRES_URL to run the postgres storage contract")
    pytest.importorskip("psycopg")
    from memspine.clients.postgres import PostgresClient
    from memspine.services.storage.postgres.engine import PostgresStorage
    from memspine.services.storage.sqlite.schema import metadata

    client = PostgresClient(url)
    try:
        await client.connect()
    except Exception as exc:  # server down / bad DSN → skip, not fail
        pytest.skip(f"postgres not reachable: {exc}")
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)  # fresh schema for isolation
    storage = PostgresStorage(client, mode=EventLogMode.ROLLING)
    await storage.start()
    return client, storage


_BACKENDS: dict[str, Callable[[], Awaitable[tuple[Any, Any]]]] = {
    "sqlite": _sqlite,
    "postgres": _postgres,
}


@pytest.mark.parametrize("backend", list(_BACKENDS))
async def test_storage_contract(backend: str) -> None:
    client, storage = await _BACKENDS[backend]()
    try:
        # append_event assigns a monotonic, gap-free seq through the write door.
        e1 = await storage.append_event(_ev("ns/a", "alpha"))
        e2 = await storage.append_event(_ev("ns/a", "beta"))
        assert e1.seq == 1 and e2.seq == 2
        events = await storage.read_events(after_seq=0)
        assert [e.seq for e in events] == [1, 2]
        assert events[0].payload == {"content": "alpha"}

        # record read-model round-trip + upsert-updates-in-place.
        await storage.upsert_record(_rec("r1", "ns/a", "content one", entity="e", attribute="a"))
        got = await storage.get_record("r1")
        assert got is not None and got.content == "content one"
        await storage.upsert_record(_rec("r1", "ns/a", "content two", entity="e", attribute="a"))
        again = await storage.get_record("r1")
        assert again is not None and again.content == "content two"  # upsert, not duplicate

        # find_active_fact (M4 incumbent) + namespace listing.
        active = await storage.find_active_fact("ns/a", "e", "a")
        assert active is not None and active.record_id == "r1"
        assert await storage.list_namespaces() == ["ns/a"]

        # offsets: advance-only high-water mark (never moves backwards), resettable.
        await storage.set_offset("proj", 5)
        assert await storage.get_offset("proj") == 5
        await storage.set_offset("proj", 3)
        assert await storage.get_offset("proj") == 5  # 3 < 5 is ignored (GREATEST/max)
        await storage.reset_offset("proj")
        assert await storage.get_offset("proj") == 0

        # M7 hard-delete drops the read-model row.
        await storage.delete_record("r1")
        assert await storage.get_record("r1") is None
    finally:
        await client.close()
