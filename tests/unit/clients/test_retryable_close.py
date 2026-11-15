"""Native connector closers keep ownership until each handle closes successfully."""

from __future__ import annotations

import asyncio
import threading
from typing import Any, cast

import pytest

from memspine.clients._common.cleanup import drain_close
from memspine.clients.aws import AwsConnector, AwsSpec
from memspine.clients.ladybug import LadybugClient, LadybugSpec
from memspine.clients.mongodb import MongoConnector, MongoSpec
from memspine.clients.postgres import PostgresClient, PostgresSpec
from memspine.clients.s3 import S3Connector, S3Spec
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.exceptions import StorageError
from memspine.providers.shared import ResourceCleanupPendingError, ResourceRegistry, resource_key


class _HandleBase:
    def __init__(self, failures: int = 0) -> None:
        self.failures = failures
        self.calls = 0

    def _attempt(self) -> None:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("native close failed")


class _Handle(_HandleBase):
    def close(self) -> None:
        self._attempt()


class _AsyncHandle(_HandleBase):
    async def close(self) -> None:
        self._attempt()

    async def dispose(self) -> None:
        self._attempt()


@pytest.mark.parametrize("failing_handle", ["connection", "database"])
async def test_ladybug_failed_close_keeps_remaining_handle_for_registry_retry(
    failing_handle: str,
) -> None:
    registry = ResourceRegistry()
    key = resource_key("graph.ladybug", {"path": "test"}, scope="thread")
    client = LadybugClient(LadybugSpec("test"))
    conn = _Handle(failures=int(failing_handle == "connection"))
    db = _Handle(failures=int(failing_handle == "database"))
    client._conn, client._db = conn, db

    async def build() -> LadybugClient:
        return client

    async def close(value: LadybugClient) -> None:
        await value.close()

    lease = await registry.acquire(key, build, close=close)
    with pytest.raises(RuntimeError, match="native close"):
        await lease.aclose()
    with pytest.raises(ResourceCleanupPendingError):
        await registry.acquire(key, build, close=close)
    assert client._conn is (conn if failing_handle == "connection" else None)
    assert client._db is db
    await registry.retry_failed_close(key)
    assert client._conn is client._db is None
    assert conn.calls == (2 if failing_handle == "connection" else 1)
    assert db.calls == (2 if failing_handle == "database" else 1)
    assert registry.stats() == ()


async def test_ladybug_cancelled_close_drains_native_thread() -> None:
    client = LadybugClient(LadybugSpec("test"))
    started = asyncio.Event()
    release_thread = threading.Event()
    loop = asyncio.get_running_loop()

    class _BlockingHandle:
        def close(self) -> None:
            loop.call_soon_threadsafe(started.set)
            assert release_thread.wait(5)

    client._conn, client._db = _BlockingHandle(), _Handle()
    closing = asyncio.create_task(client.close())
    await started.wait()
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release_thread.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert client._conn is client._db is None


async def test_double_cancel_keeps_registry_owner_until_native_close_finishes() -> None:
    registry = ResourceRegistry()
    key = resource_key("graph.ladybug", {"path": "double-cancel"}, scope="thread")
    client = LadybugClient(LadybugSpec("test"))
    started = asyncio.Event()
    release_thread = threading.Event()
    loop = asyncio.get_running_loop()
    calls = 0

    class _BlockingHandle:
        def close(self) -> None:
            nonlocal calls
            calls += 1
            loop.call_soon_threadsafe(started.set)
            assert release_thread.wait(5), "native close was not released"

    client._conn, client._db = _BlockingHandle(), _Handle()

    async def build() -> LadybugClient:
        return client

    async def close(value: LadybugClient) -> None:
        await value.close()

    lease = await registry.acquire(key, build, close=close)
    closing = asyncio.create_task(lease.aclose())
    try:
        await asyncio.wait_for(started.wait(), 5)
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done()
        closing.cancel()
        await asyncio.sleep(0)
        assert not closing.done(), "second cancellation abandoned active native close"
        with pytest.raises(ResourceCleanupPendingError, match="still closing"):
            await registry.retry_failed_close(key)
    finally:
        release_thread.set()
    with pytest.raises(asyncio.CancelledError):
        await closing
    assert calls == 1 and client._conn is client._db is None
    with pytest.raises(ResourceCleanupPendingError):
        await registry.acquire(key, build, close=close)
    await registry.retry_failed_close(key)
    assert registry.stats() == () and calls == 1


async def test_native_close_error_wins_after_repeated_cancellation() -> None:
    started, release = asyncio.Event(), asyncio.Event()

    async def failing_close() -> None:
        started.set()
        await release.wait()
        raise RuntimeError("native close failed")

    closing = asyncio.create_task(drain_close(failing_close()))
    await started.wait()
    closing.cancel()
    await asyncio.sleep(0)
    closing.cancel()
    await asyncio.sleep(0)
    assert not closing.done()
    release.set()
    with pytest.raises(RuntimeError, match="native close failed"):
        await closing


async def test_mongodb_partial_close_retries_each_remaining_handle() -> None:
    client = MongoConnector(MongoSpec("mongodb://localhost:27017"))
    native, sync = _AsyncHandle(), _Handle(failures=1)
    client._client = native  # type: ignore[assignment]
    client._sync = sync  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="native close"):
        await client.close()
    assert client._client is None and cast(Any, client)._sync is sync
    await client.close()
    assert native.calls == 1 and sync.calls == 2
    assert client._client is client._sync is None


@pytest.mark.parametrize("failing_handle", ["anchor", "engine"])
async def test_sqlite_partial_close_retries_anchor_and_engine(failing_handle: str) -> None:
    client = SQLiteClient(SQLiteSpec(":memory:"))
    anchor = _AsyncHandle(failures=int(failing_handle == "anchor"))
    engine = _AsyncHandle(failures=int(failing_handle == "engine"))
    client._anchor = anchor  # type: ignore[assignment]
    client._engine = engine  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="native close"):
        await client.close()
    assert cast(Any, client)._anchor is anchor
    assert cast(Any, client)._engine is (engine if failing_handle == "engine" else None)
    if failing_handle == "anchor":
        with pytest.raises(StorageError, match="close is incomplete"):
            await client.open()
    await client.close()
    assert anchor.calls == 1 + int(failing_handle == "anchor")
    assert engine.calls == 1 + int(failing_handle == "engine")
    assert client._anchor is client._engine is None


async def test_postgres_failed_dispose_keeps_engine_for_retry() -> None:
    client = PostgresClient(PostgresSpec("postgresql+psycopg://localhost/memspine"))
    engine = _AsyncHandle(failures=1)
    client._engine = engine  # type: ignore[assignment]
    with pytest.raises(RuntimeError, match="native close"):
        await client.close()
    assert cast(Any, client)._engine is engine
    await client.close()
    assert client._engine is None and engine.calls == 2


async def test_boto_clients_keep_handles_on_failed_close() -> None:
    aws = AwsConnector(AwsSpec("sts"))
    s3 = S3Connector(S3Spec(bucket="docs", connect_timeout_s=1, request_timeout_s=1))
    for client in (aws, s3):
        native = _Handle(failures=1)
        client._client = native
        with pytest.raises(RuntimeError, match="native close"):
            await client.close()
        assert client._client is native
        await client.close()
        assert client._client is None and native.calls == 2
