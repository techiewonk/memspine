"""RM-113: every connector implements the one protocol (identity, locality, open, real
health round trip, idempotent close, handle) and ``providers.connectors.acquire`` shares
connectors by identity through the registry."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from memspine.clients.aws import AwsConnector, AwsSpec
from memspine.clients.base import Connector
from memspine.clients.cashews import CashewsClient, CashewsSpec
from memspine.clients.http import HttpConnector, HttpError, HttpSpec, HttpTimeout
from memspine.clients.ladybug import LadybugClient, LadybugSpec
from memspine.clients.lancedb import LanceDBClient, LanceDBSpec
from memspine.clients.neo4j import Neo4jClient, Neo4jSpec
from memspine.clients.opensearch import OpenSearchClient, OpenSearchSpec
from memspine.clients.postgres import PostgresClient, PostgresSpec
from memspine.clients.s3 import S3Connector
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.clients.tantivy import TantivyConnector
from memspine.clients.weaviate import WeaviateClient, WeaviateSpec
from memspine.exceptions import StorageError
from memspine.providers import shared
from memspine.providers.connectors import acquire, http_connector
from memspine.providers.contexts import SharedBuildContext
from memspine.providers.lifecycle import ProviderLifecycleManager
from memspine.providers.shared import ResourceRegistry
from memspine.services.lexical.tantivy import tantivy_spec

PG_URL = os.environ.get("MEMSPINE_TEST_PG_URL")
ALL = (
    SQLiteClient,
    PostgresClient,
    LanceDBClient,
    LadybugClient,
    Neo4jClient,
    OpenSearchClient,
    WeaviateClient,
    CashewsClient,
    TantivyConnector,
    HttpConnector,
    AwsConnector,
    S3Connector,
)


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())


@pytest.mark.parametrize("cls", ALL, ids=lambda c: c.__name__)
def test_every_connector_declares_kind_scope_identity_locality(cls: type[Any]) -> None:
    assert isinstance(cls.kind, str) and cls.kind
    assert cls.scope in ("loop", "thread")
    for member in ("identity", "locality", "open", "health", "close", "handle", "spec"):
        assert hasattr(cls, member), member


async def test_sqlite_protocol_round_trip(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(tmp_path / "c.db"))
    assert isinstance(client, Connector)
    assert not await client.health(1.0)  # not open: False, never raises
    with pytest.raises(StorageError):
        _ = client.handle
    await client.open()
    await client.open()  # idempotent
    assert await client.health(1.0)  # SELECT 1
    assert client.handle is client.engine
    await client.close()
    await client.close()  # idempotent
    assert not await client.health(1.0)


def test_private_targets_never_share_and_files_share_by_canonical_path(tmp_path: Path) -> None:
    memory = SQLiteSpec(":memory:")
    assert SQLiteClient.identity(memory) != SQLiteClient.identity(memory)
    assert SQLiteClient.locality(memory) == "process"
    a = SQLiteClient.identity(SQLiteSpec(tmp_path / "x.db"))
    b = SQLiteClient.identity(SQLiteSpec(tmp_path / "." / "x.db"))
    assert a == b and SQLiteClient.locality(SQLiteSpec(tmp_path / "x.db")) == "host"
    assert LanceDBClient.identity(LanceDBSpec("memory://v")) != LanceDBClient.identity(
        LanceDBSpec("memory://v")
    )
    assert TantivyConnector.identity(tantivy_spec(None)) != TantivyConnector.identity(
        tantivy_spec(None)
    )


def test_sqlite_key_enters_identity_only_as_a_digest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("K1", "first-secret")
    monkeypatch.setenv("K2", "second-secret")
    one = SQLiteClient.identity(SQLiteSpec(tmp_path / "e.db", cipher_key_env="K1"))
    two = SQLiteClient.identity(SQLiteSpec(tmp_path / "e.db", cipher_key_env="K2"))
    assert one.auth_digest != two.auth_digest
    assert "first-secret" not in repr(one) and "first-secret" not in repr(one.key_target())


def test_postgres_spec_and_identity_never_show_the_password() -> None:
    spec = PostgresSpec("postgresql://user:hunter2@db.example:5433/mem")
    identity = PostgresClient.identity(spec)
    assert "hunter2" not in repr(spec) and "hunter2" not in repr(identity)
    assert identity.canonical == {
        "kind": "postgres",
        "host": "db.example",
        "port": 5433,
        "database": "mem",
    }
    assert PostgresClient.locality(spec) == "cluster"
    assert PostgresClient.locality(PostgresSpec("postgresql://u@localhost/x")) == "host"


def test_remote_identities_digest_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMSPINE_OPENSEARCH_URL", "https://search.example:9200")
    monkeypatch.setenv("OS_U", "alice")
    monkeypatch.setenv("OS_P", "pw-a")
    spec = OpenSearchSpec(1, 1, username_env="OS_U", password_env="OS_P")
    first = OpenSearchClient.identity(spec)
    monkeypatch.setenv("OS_P", "pw-b")
    second = OpenSearchClient.identity(spec)
    assert first.target == second.target and first.auth_digest != second.auth_digest
    assert "pw-a" not in repr(first)
    assert OpenSearchClient.locality(spec) == "cluster"
    monkeypatch.setenv("MEMSPINE_WEAVIATE_URL", "http://localhost:8080")
    assert WeaviateClient.locality(WeaviateSpec(1, 1)) == "host"
    monkeypatch.setenv("MEMSPINE_NEO4J_URL", "neo4j://user:pw@graph.example:7687")
    target = Neo4jClient.identity(Neo4jSpec(1, 1)).descriptor_target()
    assert "pw" not in str(target["canonical_target"]) and target["port"] == 7687
    cache = CashewsClient.identity(CashewsSpec("redis://:s3cret@cache.example:6379/0"))
    assert "s3cret" not in repr(cache) and cache.auth_digest
    assert CashewsClient.locality(CashewsSpec("mem://")) == "process"


async def test_lancedb_and_tantivy_health_are_real_round_trips(tmp_path: Path) -> None:
    lance = LanceDBClient(LanceDBSpec(tmp_path / "v.lance"))
    assert not await lance.health(1.0)
    await lance.open()
    assert await lance.health(5.0)
    await lance.close()
    index = TantivyConnector(tantivy_spec(tmp_path / "idx"))
    assert not await index.health(1.0)
    await index.open()
    assert await index.health(5.0)
    await index.close()
    await index.close()
    with pytest.raises(StorageError):
        _ = index.writer  # the writer (and its lock on the directory) is released
    fresh = TantivyConnector(tantivy_spec(tmp_path / "never-opened"))
    await fresh.close()
    with pytest.raises(RuntimeError, match="closed"):
        await fresh.open()  # a closed connector never takes the writer lock afterwards


async def test_ladybug_health_runs_return_one() -> None:
    pytest.importorskip("ladybug", reason="LadybugDB [graph] extra is not installed")
    client = LadybugClient(LadybugSpec())
    await client.open()
    assert await client.health(5.0)
    await client.close()
    await client.close()


async def test_cashews_health_is_a_get_round_trip(tmp_path: Path) -> None:
    pytest.importorskip("cashews", reason="cashews [cache] extra is not installed")
    client = CashewsClient(CashewsSpec("mem://"))
    await client.open()
    assert await client.health(1.0)
    await client.close()
    assert not await client.health(1.0)


async def test_http_connector_never_follows_redirects_and_maps_errors() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/slow":
            raise httpx.ReadTimeout("slow", request=request)
        if request.url.path == "/down":
            raise httpx.ConnectError("refused", request=request)
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"Location": "https://other.example/"})
        return httpx.Response(200, content=b"x" * 50)

    http = HttpConnector(
        HttpSpec(base_url="https://svc.example", transport=httpx.MockTransport(handler))
    )
    reply = await http.request("GET", "https://svc.example/moved")
    assert reply.is_redirect and reply.headers["location"] == "https://other.example/"
    capped = await http.request("GET", "https://svc.example/big", max_bytes=10)
    assert capped.truncated and len(capped.body) <= 50
    with pytest.raises(HttpTimeout):
        await http.request("GET", "https://svc.example/slow")
    with pytest.raises(HttpError):
        await http.request("GET", "https://svc.example/down")
    assert await http.health(1.0)  # GET base_url < 500
    await http.close()
    await http.close()
    assert not await http.health(1.0)
    assert (await http.request("GET", "https://svc.example/again")).status == 200  # reopens
    await http.close()
    assert http_connector().spec.max_connections is None


async def test_aws_connector_opens_without_network_and_health_uses_sts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class Client:
        def __init__(self, service: str) -> None:
            self.service = service

        def get_caller_identity(self) -> dict[str, str]:
            calls.append(self.service)
            return {"Account": "123"}

        def close(self) -> None:
            calls.append(f"close:{self.service}")

    class Session:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs

        def client(self, service: str) -> Client:
            return Client(service)

    fake = SimpleNamespace(session=SimpleNamespace(Session=Session))
    monkeypatch.setitem(sys.modules, "boto3", fake)
    connector = AwsConnector(AwsSpec("secretsmanager", region_name="eu-west-1"))
    connector.open_blocking()
    assert connector.handle.service == "secretsmanager"
    assert await connector.health(1.0)
    assert calls[0] == "sts"  # never the paid service API
    await connector.close()
    await connector.close()
    assert "close:secretsmanager" in calls


async def test_acquire_shares_by_identity_and_closes_on_last_release(tmp_path: Path) -> None:
    first, second = (
        ProviderLifecycleManager(close_timeout_s=5.0),
        ProviderLifecycleManager(close_timeout_s=5.0),
    )
    spec = LanceDBSpec(tmp_path / "shared.lance")
    a, _ = await acquire(SharedBuildContext(first, "o", 1.0), LanceDBClient, spec, resource_id="v")
    b, _ = await acquire(
        SharedBuildContext(second, "o", 1.0),
        LanceDBClient,
        LanceDBSpec(tmp_path / "." / "shared.lance"),
        resource_id="v",
    )
    other, _ = await acquire(
        SharedBuildContext(second, "o", 1.0),
        LanceDBClient,
        LanceDBSpec(tmp_path / "other.lance"),
        resource_id="v2",
    )
    assert a is b and other is not a
    assert (await first.close_all()).complete
    assert await b.health(5.0)  # the other holder keeps it open
    assert (await second.close_all()).complete
    assert not await b.health(1.0)


@pytest.mark.skipif(not PG_URL, reason="set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")
def test_postgres_migrations_serialize_on_the_advisory_lock() -> None:
    """ADR-081: two concurrent migration runs on one database; the lock serialises them and
    both end at head (the second finds nothing to do)."""
    import threading

    from sqlalchemy import text

    from memspine.clients.postgres.sync import MIGRATION_LOCK_KEY, advisory_lock
    from memspine.services.storage.postgres import migrations

    assert PG_URL is not None
    client = PostgresClient(PostgresSpec(PG_URL, connect_timeout_s=10))
    held = threading.Event()
    release = threading.Event()
    order: list[str] = []

    def holder() -> None:
        with client.migration_connection() as connection, advisory_lock(connection):
            held.set()
            release.wait(10)
            order.append("holder")

    def migrator() -> None:
        with client.migration_connection() as connection:
            migrations.ensure_schema(connection)
            order.append("migrator")

    t1 = threading.Thread(target=holder)
    t1.start()
    assert held.wait(10)
    t2 = threading.Thread(target=migrator)
    t2.start()
    t2.join(1.0)
    assert t2.is_alive()  # waiting on the advisory lock
    release.set()
    t1.join(10)
    t2.join(60)
    assert order == ["holder", "migrator"]
    with client.migration_connection() as connection:
        version = connection.execute(text("SELECT version_num FROM alembic_version")).scalar()
        assert version == migrations.heads()[0]
        locks = connection.execute(
            text("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory' AND objid = :k"),
            {"k": MIGRATION_LOCK_KEY & 0xFFFFFFFF},
        ).scalar()
        assert locks == 0  # released


@pytest.mark.skipif(not PG_URL, reason="set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")
def test_postgres_health_is_select_one() -> None:
    assert PG_URL is not None
    client = PostgresClient(PostgresSpec(PG_URL, connect_timeout_s=10))

    async def run() -> tuple[bool, bool]:
        await client.open()
        healthy = await client.health(5.0)
        await client.close()
        return healthy, await client.health(1.0)

    assert asyncio.run(run(), loop_factory=asyncio.SelectorEventLoop) == (True, False)
