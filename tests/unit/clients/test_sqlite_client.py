"""SQLite client: pooling + shared-cache in-memory semantics."""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.pool import StaticPool

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec


async def test_memory_engine_gets_a_real_pool_not_staticpool() -> None:
    """Regression (empirically found): SQLAlchemy's aiosqlite dialect silently
    selects StaticPool for mode=memory URLs — one shared connection whose
    concurrent transactions interleave. The client must force a real pool."""
    client = SQLiteClient(SQLiteSpec(":memory:"))
    await client.open()
    try:
        assert not isinstance(client.engine.pool, StaticPool)
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


@pytest.mark.filterwarnings("ignore::sqlalchemy.exc.SAWarning")
async def test_shared_cache_database_survives_pool_churn() -> None:
    """The anchor connection keeps the named in-memory DB alive even when all
    pooled connections have come and gone (dispose() warns about the checked-
    out anchor — expected here, we dispose deliberately mid-lifecycle)."""
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


async def test_configured_busy_timeout_is_applied_to_connections(tmp_path) -> None:
    client = SQLiteClient(SQLiteSpec(tmp_path / "busy.db", busy_timeout_s=0.125))
    await client.open()
    try:
        async with client.engine.connect() as conn:
            value = await conn.scalar(text("PRAGMA busy_timeout"))
        assert value == 125
    finally:
        await client.close()
