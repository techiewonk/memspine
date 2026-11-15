"""The private sync lease database belongs to one in-memory SQLite storage instance."""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy.exc import OperationalError

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store import leases as lease_module
from memspine.store.leases import LeaseStore


async def test_memory_lease_threaded_claims_serialize_one_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lease_module, "_sqlite_connection_factory", None)
    client = SQLiteClient(SQLiteSpec(":memory:"))
    store = SQLiteStorage(client)
    await client.open()
    try:
        await store.start()
        # The scheduler calls these methods via asyncio.to_thread. The sync
        # in-memory engine uses one DB-API connection, even across worker threads.
        leases = store.leases()
        assert isinstance(leases, LeaseStore)
        assert leases.offload
        handles = await asyncio.gather(*(asyncio.to_thread(store.leases) for _ in range(32)))
        assert len({id(item) for item in handles}) == 1

        same = await asyncio.gather(
            *(
                asyncio.to_thread(leases.claim, "same", f"worker-{n}", f"job-{n}", 30.0)
                for n in range(64)
            )
        )
        winners = [lease for lease in same if lease is not None]
        assert len(winners) == 1
        winner = winners[0]
        assert leases.get_lease("same") == winner

        distinct = await asyncio.gather(
            *(
                asyncio.to_thread(leases.claim, f"ns-{n}", "worker", f"job-{n}", 30.0)
                for n in range(64)
            )
        )
        assert all(lease is not None and lease.epoch == 1 for lease in distinct)
        assert leases.release(winner)
        successor = leases.claim("same", "next", "next-job", 30.0)
        assert successor is not None and successor.epoch == winner.epoch + 1
        assert await asyncio.to_thread(leases.heartbeat, winner, 30.0) is None
    finally:
        await store.stop()
        await client.close()


async def test_memory_lease_stores_are_independent_and_disposed_on_stop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(lease_module, "_sqlite_connection_factory", None)
    clients = [SQLiteClient(SQLiteSpec(":memory:")) for _ in range(2)]
    stores = [SQLiteStorage(client) for client in clients]
    try:
        for client, store in zip(clients, stores, strict=True):
            await client.open()
            await store.start()

        first, second = (store.leases() for store in stores)
        assert first is stores[0].leases()  # stable while the store is running
        assert first.get_lease("same") is None
        a = first.claim("same", "a", "job-a", ttl=30)
        assert a is not None and a.epoch == 1
        assert first.claim("same", "other", "job", ttl=30) is None
        assert second.get_lease("same") is None
        b = second.claim("same", "b", "job-b", ttl=30)
        assert b is not None and b.epoch == 1

        assert first.release(a)
        successor = first.claim("same", "other", "job", ttl=30)
        assert successor is not None and successor.epoch == 2
        assert first.heartbeat(a, ttl=30) is None  # stale epoch stays fenced
        assert second.get_lease("same") == b

        old_engine = stores[0]._lease_engine
        assert old_engine is not None
        await stores[0].stop()
        assert stores[0]._lease_engine is None
        assert stores[0]._leases is None
        # StaticPool's sole connection was closed by dispose(): its in-memory
        # tables cannot be reached even through a retained engine reference.
        with pytest.raises(OperationalError), old_engine.connect() as connection:
            connection.exec_driver_sql("SELECT count(*) FROM sleep_lease")
        assert second.claim("same", "other", "job", ttl=30) is None

        await stores[0].start()
        fresh = stores[0].leases()
        assert fresh.get_lease("same") is None
        assert fresh.claim("same", "fresh", "job", ttl=30) is not None
        assert second.get_lease("same") == b
    finally:
        for store, client in zip(stores, clients, strict=True):
            await store.stop()
            await client.close()
