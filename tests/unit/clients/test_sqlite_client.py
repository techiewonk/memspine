"""SQLite client: pooling + shared-cache in-memory semantics."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import aiosqlite.core
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine
from sqlalchemy.pool import StaticPool

import memspine.clients.sqlite.connector as sqlite_connector
from memspine import Engine
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.schemas.durable.common import OwnerScope
from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph
from memspine.services.storage.records_port import OutboxItem
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, MemoryEvent


async def test_memory_engine_gets_a_real_pool_not_staticpool() -> None:
    """Regression (empirically found): SQLAlchemy's aiosqlite dialect silently
    selects StaticPool for mode=memory URLs — one shared connection whose
    concurrent transactions interleave. The client must force a real pool."""
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    try:
        assert not isinstance(client.engine.pool, StaticPool)
        assert client.engine.pool.size() == 1
    finally:
        await client.close()


async def test_two_memory_clients_are_isolated() -> None:
    a, b = SQLiteClient(SQLiteSpec(":memory:")), SQLiteClient(SQLiteSpec(":memory:"))
    await a.open()
    await b.open()
    try:
        async with a.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (x INTEGER)"))
        async with b.engine.connect() as conn:
            tables = (
                await conn.execute(
                    text("SELECT name FROM sqlite_master WHERE type='table' AND name='t'")
                )
            ).all()
        assert tables == []  # b never sees a's shared-cache database
    finally:
        await a.close()
        await b.close()


async def test_shared_cache_database_survives_pool_churn() -> None:
    """The anchor connection keeps the named in-memory DB alive even when all
    operational pooled connections have come and gone."""
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    try:
        async with client.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (x INTEGER)"))
            await conn.execute(text("INSERT INTO t VALUES (1)"))
        await client.engine.dispose()  # drain every pooled connection
        async with client.engine.connect() as conn:
            rows = (await conn.execute(text("SELECT x FROM t"))).all()
        assert [tuple(row) for row in rows] == [(1,)]
    finally:
        await client.close()


async def test_memory_queue_wait_cancel_then_next_transaction_succeeds() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    entered, release = asyncio.Event(), asyncio.Event()

    async def holder() -> None:
        async with client.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (x INTEGER)"))
            entered.set()
            await release.wait()
            await conn.execute(text("INSERT INTO t VALUES (1)"))

    async def queued() -> None:
        async with client.engine.begin() as conn:
            await conn.execute(text("INSERT INTO t VALUES (2)"))

    held = asyncio.create_task(holder())
    try:
        await asyncio.wait_for(entered.wait(), 5)
        waiting = asyncio.create_task(queued())
        await asyncio.sleep(0)  # let the second task queue at the only pool slot
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        release.set()
        await asyncio.wait_for(held, 5)
        await asyncio.wait_for(queued(), 5)
        async with client.engine.connect() as conn:
            assert (await conn.execute(text("SELECT x FROM t ORDER BY x"))).scalars().all() == [
                1,
                2,
            ]
    finally:
        release.set()
        if not held.done():
            held.cancel()
        await asyncio.gather(held, return_exceptions=True)
        await client.close()


async def test_memory_cancelled_transaction_rolls_back_and_releases_slot() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    entered = asyncio.Event()
    try:
        async with client.engine.begin() as conn:
            await conn.execute(text("CREATE TABLE t (x INTEGER)"))

        async def cancelled_writer() -> None:
            async with client.engine.begin() as conn:
                await conn.execute(text("INSERT INTO t VALUES (1)"))
                entered.set()
                await asyncio.Event().wait()

        writer = asyncio.create_task(cancelled_writer())
        await asyncio.wait_for(entered.wait(), 5)
        writer.cancel()
        with pytest.raises(asyncio.CancelledError):
            await writer
        async with asyncio.timeout(5):
            async with client.engine.begin() as conn:
                await conn.execute(text("INSERT INTO t VALUES (2)"))
            async with client.engine.connect() as conn:
                assert (await conn.execute(text("SELECT x FROM t"))).scalars().all() == [2]
    finally:
        await client.close()


async def test_memory_close_reopen_starts_a_fresh_private_database() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    async with client.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (x INTEGER)"))
    await client.close()
    await client.open()
    try:
        async with client.engine.connect() as conn:
            assert (
                await conn.scalar(
                    text("SELECT count(*) FROM sqlite_master WHERE type='table' AND name='t'")
                )
                == 0
            )
    finally:
        await client.close()


async def test_cancelled_open_closes_anchor_even_when_connection_completes_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = sqlite3.connect
    connected, release = threading.Event(), threading.Event()
    raw_connections: list[sqlite3.Connection] = []

    def blocked_connect(*args: object, **kwargs: object) -> sqlite3.Connection:
        # The worker has created the native handle, but aiosqlite has not yet
        # assigned it to Connection._connection when the caller is cancelled.
        connection = real_connect(*args, **{**kwargs, "check_same_thread": False})
        raw_connections.append(connection)
        connected.set()
        if not release.wait(5):
            raise TimeoutError("test connect barrier expired")
        return connection

    monkeypatch.setattr(aiosqlite.core.sqlite3, "connect", blocked_connect)
    client = SQLiteClient(SQLiteSpec(":memory:"))
    opening = asyncio.create_task(client.open())
    try:
        assert await asyncio.wait_for(asyncio.to_thread(connected.wait, 5), 6)
        opening.cancel()
        await asyncio.sleep(0)
        opening.cancel()  # cancellation while cleanup is draining must still close
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(opening, 5)
        assert len(raw_connections) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            raw_connections[0].execute("SELECT 1")
        assert client._engine is client._anchor is None
    finally:
        release.set()
        await asyncio.gather(opening, return_exceptions=True)
        monkeypatch.setattr(aiosqlite.core.sqlite3, "connect", real_connect)
    await client.open()
    try:
        assert await client.health(5)
    finally:
        await client.close()


async def test_failed_engine_dispose_keeps_anchor_and_database_for_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    async with client.engine.begin() as conn:
        await conn.execute(text("CREATE TABLE t (x INTEGER)"))
        await conn.execute(text("INSERT INTO t VALUES (7)"))
    original = AsyncEngine.dispose
    failed = False

    async def fail_after_dispose(engine: AsyncEngine, close: bool = True) -> None:
        nonlocal failed
        await original(engine, close=close)
        if not failed:
            failed = True
            raise RuntimeError("injected dispose failure")

    monkeypatch.setattr(AsyncEngine, "dispose", fail_after_dispose)
    with pytest.raises(RuntimeError, match="injected dispose failure"):
        await client.close()
    assert client._anchor is not None
    async with client.engine.connect() as conn:
        assert await conn.scalar(text("SELECT x FROM t")) == 7
    await client.close()
    assert client._engine is client._anchor is None


async def test_memory_open_probe_failure_closes_anchor_then_retry_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_connect = sqlite_connector.aiosqlite.connect
    real_probe = sqlite_connector._first_connection
    anchors: list[aiosqlite.core.Connection] = []

    async def recording_connect(*args: object, **kwargs: object) -> aiosqlite.core.Connection:
        anchor = await real_connect(*args, **kwargs)
        anchors.append(anchor)
        return anchor

    async def fail_probe(_engine: AsyncEngine, _timeout: int) -> None:
        raise RuntimeError("injected operational probe failure")

    monkeypatch.setattr(sqlite_connector.aiosqlite, "connect", recording_connect)
    monkeypatch.setattr(sqlite_connector, "_first_connection", fail_probe)
    client = SQLiteClient(SQLiteSpec(":memory:"))
    with pytest.raises(RuntimeError, match="injected operational probe failure"):
        await client.open()
    assert len(anchors) == 1 and anchors[0]._connection is None
    assert client._engine is client._anchor is None
    monkeypatch.setattr(sqlite_connector.aiosqlite, "connect", real_connect)
    monkeypatch.setattr(sqlite_connector, "_first_connection", real_probe)
    await client.open()
    try:
        assert await client.health(5)
    finally:
        await client.close()


async def test_memory_records_graph_and_outbox_share_one_operational_pool() -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    storage = SQLiteStorage(client)
    await storage.start()
    graph = SqlAdjacencyGraph(client)
    scope = OwnerScope(tenant="t", owner="alice")
    try:
        async with asyncio.timeout(10):
            event, offsets = await storage.append_with_outbox(
                MemoryEvent(kind=EventKind.MARKER, namespace="t/alice", payload={"marker": "test"}),
                [OutboxItem(scope, "windows", {"turn": 1})],
            )
            await graph.upsert_edge("r1", "r2", "related")
            assert event.seq == 1 and offsets == [0]
            assert await storage.outbox_items(scope, "windows") == [(0, {"turn": 1})]
            assert [node.node_id for node in await graph.neighbors("r1")] == ["r2"]
            assert [item.event_id for item in await storage.read_events()] == [event.event_id]
    finally:
        await storage.stop()
        await client.close()


async def test_memory_core_engine_ingest_search_read_finishes_with_bounded_pool() -> None:
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": ":memory:"}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
    )
    async with asyncio.timeout(20):
        await engine.start()
        try:
            [turn] = await engine.ingest_messages(
                [{"message_id": "m1", "role": "user", "content": "Alice drinks green tea"}],
                namespace="a",
            )
            hits = await engine.search("green tea", namespace="a")
            assert turn.record_id in {record.record_id for record, _score in hits}
            result = await engine.read("green tea", namespace="a", mode="retrieve")
            assert turn.record_id in {record.record_id for record in result.context.records}
        finally:
            await engine.stop()


async def test_configured_busy_timeout_is_applied_to_connections(tmp_path) -> None:
    client = SQLiteClient(SQLiteSpec(tmp_path / "busy.db", busy_timeout_s=0.125))
    await client.open()
    try:
        async with client.engine.connect() as conn:
            value = await conn.scalar(text("PRAGMA busy_timeout"))
        assert value == 125
    finally:
        await client.close()
