"""RM-31 finite Memspine connector-owned native pool lifecycle soak on the owned RM-136 stack.

No containers or shared data are changed. Run from the existing Python environment:
    python scripts/check_remote_provider_resource_soak.py --out docs/provider_verification/rm31-native-pool-soak-<run>.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from urllib.request import urlopen

import rm31_owned_services as isolated_services
from local_services import load_state, owned_containers, ports_for, run
from pymongo.monitoring import ConnectionPoolListener

ROOT = Path(__file__).resolve().parents[1]
WARMUPS = 5
MEASURED = 30
WALL_CAP_S = 1500.0
SETTLE_S = 0.2
BASE_SERVICES = ("postgres", "redis", "valkey", "mongo", "neo4j")
SERVICES = (*BASE_SERVICES, "opensearch", "weaviate")
ENGINE_ROWS = (
    "postgres_records",
    "redis_cache",
    "valkey_cache",
    "mongo_records",
    "neo4j_graph",
    "opensearch_lexical",
    "weaviate_vector_lexical",
)


@contextmanager
def exclusive_run() -> Any:
    """OS-held single-run lock, automatically released on process exit."""
    lock_path = Path(tempfile.gettempdir()) / "memspine-rm31-native-soak.lock"
    with lock_path.open("a+b") as handle:
        handle.seek(0)
        handle.write(b"0")
        handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            try:
                handle.truncate(1)
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)  # type: ignore[attr-defined]
            try:
                handle.truncate(1)
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)  # type: ignore[attr-defined]


def slope(values: list[int | None]) -> float | None:
    numbers = [float(v) for v in values if v is not None]
    if len(numbers) != len(values) or len(numbers) < 2:
        return None
    mid = (len(numbers) - 1) / 2
    average = sum(numbers) / len(numbers)
    return sum((i - mid) * (v - average) for i, v in enumerate(numbers)) / sum(
        (i - mid) ** 2 for i in range(len(numbers))
    )


def summarize(samples: list[dict[str, Any]]) -> dict[str, Any]:
    measured = [s for s in samples if s["phase"] == "measured"]
    failures = [s for s in samples if s["error_type"]]
    proved = [s for s in samples if s["telemetry_available"]]
    return {
        "warmups_completed": sum(s["phase"] == "warmup" and not s["error_type"] for s in samples),
        "measured_completed": sum(not s["error_type"] for s in measured),
        "failure_types": [s["error_type"] for s in failures],
        "telemetry_coverage": len(proved),
        "owned_outstanding_zero_after_settle": (
            all(s["owned_outstanding_after_settle"] == 0 for s in proved)
            if proved and len(proved) == len(samples)
            else None
        ),
        "rss_post_stop_slope_bytes_per_cycle": slope([s["rss_after_stop"] for s in measured]),
        "handle_post_stop_slope_per_cycle": slope([s["handles_after_stop"] for s in measured]),
    }


def rss_bytes() -> int | None:
    try:
        import psutil

        return int(psutil.Process().memory_info().rss)
    except ImportError:
        return None


def handle_count() -> int | None:
    try:
        import psutil

        process = psutil.Process()
        return int(process.num_handles() if os.name == "nt" else process.num_fds())
    except (AttributeError, ImportError):
        return None


class MongoPoolEvents(ConnectionPoolListener):
    """Per-client PyMongo pool events, excluding observer clients."""

    def __init__(self) -> None:
        self.checked_out = 0
        self.created = 0
        self.closed = 0
        self.checkout_events = 0
        self.checkin_events = 0

    def pool_created(self, event: Any) -> None:
        pass

    def pool_ready(self, event: Any) -> None:
        pass

    def pool_cleared(self, event: Any) -> None:
        pass

    def pool_closed(self, event: Any) -> None:
        pass

    def connection_created(self, event: Any) -> None:
        self.created += 1

    def connection_ready(self, event: Any) -> None:
        pass

    def connection_closed(self, event: Any) -> None:
        self.closed += 1

    def connection_check_out_started(self, event: Any) -> None:
        pass

    def connection_check_out_failed(self, event: Any) -> None:
        pass

    def connection_checked_out(self, event: Any) -> None:
        self.checked_out += 1
        self.checkout_events += 1

    def connection_checked_in(self, event: Any) -> None:
        self.checked_out -= 1
        self.checkin_events += 1

    def sample(self) -> dict[str, int]:
        return {
            "created": self.created,
            "closed": self.closed,
            "checkout_events": self.checkout_events,
            "checkin_events": self.checkin_events,
            "checked_out": self.checked_out,
            "open_created_connections": self.created - self.closed,
        }


async def postgres_connector_cycle(port: int, password: str, run_id: str) -> dict[str, Any]:
    from sqlalchemy import text

    from memspine.clients.postgres.connector import PostgresClient, PostgresSpec

    url = f"postgresql://memspine_local:{password}@127.0.0.1:{port}/memspine_local?application_name=rm31-{run_id}"
    client = PostgresClient(
        PostgresSpec(
            url,
            connect_timeout_s=3,
            statement_timeout_s=3,
        )
    )
    try:
        await client.open()
        pool: Any = client.engine.sync_engine.pool
        async with client.engine.connect() as connection:
            assert await connection.scalar(text("SELECT 1")) == 1
        before = {"checked_out": pool.checkedout(), "checked_in": pool.checkedin()}
    finally:
        await client.close()
    after = {"checked_out": pool.checkedout(), "checked_in": pool.checkedin()}
    return {
        "telemetry_available": True,
        "owned_outstanding_after_settle": sum(after.values()),
        "telemetry_before_close": before,
        "telemetry_after_close": after,
        "ownership": "memspine.PostgresClient-owned SQLAlchemy pool",
    }


async def cashews_connector_cycle(
    port: int, provider: str, run_id: str, cycle: int
) -> dict[str, Any]:
    from memspine.clients.cashews import CashewsClient, CashewsSpec

    client = CashewsClient(
        CashewsSpec(f"redis://127.0.0.1:{port}/0?client_name=rm31-{provider}-{run_id}")
    )
    key = f"rm31-{provider}-{run_id}-{cycle}"
    cache: Any = None
    try:
        await client.open()
        cache = client.handle
        backend = next(iter(cache._backends.values()))
        pool = backend._client.connection_pool
        await cache.set(key, "owned", expire=30)
        assert await cache.get(key) == "owned"
        await cache.delete(key)
        before = {
            "in_use": len(pool._in_use_connections),
            "available": len(pool._available_connections),
        }
    finally:
        try:
            if cache is not None:
                await cache.delete(key)
        finally:
            await client.close()
    after = {
        "in_use": len(pool._in_use_connections),
        "available": len(pool._available_connections),
        "connected_sockets": sum(
            bool(connection.is_connected)
            for connection in (*pool._in_use_connections, *pool._available_connections)
        ),
    }
    return {
        "telemetry_available": True,
        "owned_outstanding_after_settle": after["in_use"] + after["connected_sockets"],
        "telemetry_before_close": before,
        "telemetry_after_close": after,
        "ownership": "memspine.CashewsClient-owned redis.asyncio pool",
    }


async def mongo_connector_cycle(port: int, run_id: str, events: MongoPoolEvents) -> dict[str, Any]:
    from memspine.clients.mongodb.connector import MongoConnector, MongoSpec

    client = MongoConnector(
        MongoSpec(
            f"mongodb://127.0.0.1:{port}/?directConnection=true",
            database=f"rm31_{run_id}",
            app_name=f"rm31-{run_id}",
            connect_timeout_s=3,
            server_selection_timeout_s=3,
        )
    )
    prior = events.sample()

    def sync_ping() -> Any:
        database = client.sync_database()
        assert database.client.admin.command("ping")["ok"] == 1
        return database.client

    def pool_sample(pool: Any) -> dict[str, int]:
        return {
            "idle": len(pool.conns),
            "active_sockets": int(pool.active_sockets),
            "requests": int(pool.requests),
        }

    try:
        await client.open()
        async_pool = next(iter(client.handle._topology._servers.values())).pool
        assert await client.health(3)
        sync_client = await asyncio.to_thread(sync_ping)
        sync_pool = next(iter(sync_client._topology._servers.values())).pool
        before = {
            "async_pool": pool_sample(async_pool),
            "sync_pool": pool_sample(sync_pool),
            "listener": events.sample(),
        }
    finally:
        await client.close()
    await asyncio.sleep(SETTLE_S)
    current = events.sample()
    after_async = pool_sample(async_pool)
    after_sync = pool_sample(sync_pool)
    listener_open_delta = (current["created"] - prior["created"]) - (
        current["closed"] - prior["closed"]
    )
    after = {
        "async_pool": after_async,
        "sync_pool": after_sync,
        "listener_delta_created": current["created"] - prior["created"],
        "listener_delta_closed": current["closed"] - prior["closed"],
        "listener_checked_out": current["checked_out"],
        "listener_open_delta": listener_open_delta,
    }
    return {
        "telemetry_available": True,
        "owned_outstanding_after_settle": sum(after_async.values())
        + sum(after_sync.values())
        + current["checked_out"]
        + listener_open_delta,
        "telemetry_before_close": before,
        "telemetry_after_close": after,
        "ownership": "memspine.MongoConnector-owned async and on-demand sync PyMongo pools",
    }


async def neo4j_connector_cycle() -> dict[str, Any]:
    from memspine.clients.neo4j import Neo4jClient, Neo4jSpec

    client = Neo4jClient(
        Neo4jSpec(
            connect_timeout_s=3,
            request_timeout_s=3,
            url_env="RM31_NEO4J_URL",
            user_env="RM31_NEO4J_USER",
            password_env="RM31_NEO4J_PASSWORD",
        )
    )
    try:
        await client.open()
        pool = client.handle._pool
        assert await client.health(3)
        before = {"pooled_sockets": sum(len(group) for group in pool.connections.values())}
    finally:
        await client.close()
    await asyncio.sleep(SETTLE_S)
    after = {"pooled_sockets": sum(len(group) for group in pool.connections.values())}
    return {
        "telemetry_available": True,
        "owned_outstanding_after_settle": after["pooled_sockets"],
        "telemetry_before_close": before,
        "telemetry_after_close": after,
        "ownership": "memspine.Neo4jClient-owned native driver pool",
    }


async def opensearch_connector_cycle() -> dict[str, Any]:
    from memspine.clients.opensearch import OpenSearchClient, OpenSearchSpec

    client = OpenSearchClient(OpenSearchSpec(10, 10, url_env="RM31_OPENSEARCH_URL"))
    try:
        await client.open()
        connections = client.handle.transport.connection_pool.connections
        if len(connections) != 1 or connections[0].session is None:
            raise RuntimeError("OpenSearch native session telemetry unavailable")
        session = connections[0].session
        pool = session.connector
        if pool is None:
            raise RuntimeError("OpenSearch aiohttp pool telemetry unavailable")
        assert await client.health(10)
        before = {
            "session_closed": session.closed,
            "pool_closed": pool.closed,
            "acquired": len(pool._acquired),
            "idle": sum(len(group) for group in pool._conns.values()),
        }
    finally:
        await client.close()
    after = {
        "session_closed": session.closed,
        "pool_closed": pool.closed,
        "acquired": len(pool._acquired),
        "idle": sum(len(group) for group in pool._conns.values()),
    }
    return {
        "telemetry_available": True,
        "owned_outstanding_after_settle": after["acquired"]
        + after["idle"]
        + int(not after["session_closed"])
        + int(not after["pool_closed"]),
        "telemetry_before_close": before,
        "telemetry_after_close": after,
        "ownership": "memspine.OpenSearchClient-owned aiohttp session and pool",
    }


async def weaviate_connector_cycle(grpc_port: int, run_id: str, cycle: int) -> dict[str, Any]:
    from memspine.clients.weaviate import WeaviateClient, WeaviateSpec
    from memspine.services._weaviate import (
        LEXICAL_PROPERTIES,
        WeaviateCollectionBinding,
        collection_name,
    )
    from memspine.services.lexical.weaviate_store import WeaviateLexicalStore

    client = WeaviateClient(WeaviateSpec(10, 10, url_env="RM31_WEAVIATE_URL", grpc_port=grpc_port))
    name = collection_name("Rm31", "Probe", f"{run_id}-{cycle}", "active")
    try:
        await client.open()
        connection = client.handle._connection
        http = connection._client
        grpc = connection._grpc_channel
        if http is None or grpc is None or not hasattr(http._transport, "_pool"):
            raise RuntimeError("Weaviate native HTTP/gRPC telemetry unavailable")
        store = WeaviateLexicalStore(
            client, WeaviateCollectionBinding(name, LEXICAL_PROPERTIES), owner_id=f"rm31-{run_id}"
        )
        assert await client.health(10)
        await store.start()
        await store._owned.query.fetch_objects(limit=1)
        before = {
            "http_closed": http.is_closed,
            "http_connections": len(http._transport._pool.connections),
            "grpc_state": grpc.get_state().name,
            "grpc_query_completed": True,
        }
        if before["grpc_state"] != "READY":
            raise RuntimeError("Weaviate gRPC channel was not READY after query")
    finally:
        try:
            if getattr(
                client, "_client", None
            ) is not None and await client.handle.collections.exists(name):
                await client.handle.collections.delete(name)
        finally:
            await client.close()
    after = {
        "http_closed": http.is_closed,
        "http_connections": len(http._transport._pool.connections),
        "grpc_state": grpc.get_state().name,
    }
    return {
        "telemetry_available": True,
        "owned_outstanding_after_settle": after["http_connections"]
        + int(not after["http_closed"])
        + int(after["grpc_state"] != "SHUTDOWN"),
        "telemetry_before_close": before,
        "telemetry_after_close": after,
        "ownership": "memspine.WeaviateClient-owned httpx pool and gRPC channel",
    }


async def run_cycles(
    data: dict[str, object], isolated: dict[str, Any], deadline: float, run_id: str
) -> dict[str, Any]:
    from pymongo import monitoring

    ports = ports_for(data)
    password = str(data["password"])
    results: dict[str, Any] = {}
    mongo_events = MongoPoolEvents()
    monitoring.register(mongo_events)
    for service in SERVICES:
        samples: list[dict[str, Any]] = []
        for cycle in range(WARMUPS + MEASURED):
            sample: dict[str, Any] = {
                "cycle": cycle,
                "phase": "warmup" if cycle < WARMUPS else "measured",
                "rss_before": rss_bytes(),
                "handles_before": handle_count(),
                "error_type": None,
                "telemetry_available": False,
                "owned_outstanding_after_settle": None,
            }
            started = time.monotonic()
            try:
                if started >= deadline:
                    raise TimeoutError("global wall cap reached")
                async with asyncio.timeout(min(15.0, deadline - started)):
                    if service == "postgres":
                        observation = await postgres_connector_cycle(
                            ports["POSTGRES"], password, run_id
                        )
                    elif service in ("redis", "valkey"):
                        observation = await cashews_connector_cycle(
                            ports[service.upper()], service, run_id, cycle
                        )
                    elif service == "mongo":
                        observation = await mongo_connector_cycle(
                            ports["MONGO"], run_id, mongo_events
                        )
                    elif service == "neo4j":
                        observation = await neo4j_connector_cycle()
                    elif service == "opensearch":
                        observation = await opensearch_connector_cycle()
                    else:
                        observation = await weaviate_connector_cycle(
                            isolated["weaviate"]["ports"]["50051"], run_id, cycle
                        )
                    sample.update(observation)
            except BaseException as exc:
                sample["error_type"] = type(exc).__name__
            sample["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            sample["rss_after_stop"] = rss_bytes()
            sample["handles_after_stop"] = handle_count()
            samples.append(sample)
            if sample["error_type"] or sample["owned_outstanding_after_settle"] != 0:
                break
        results[service] = {"summary": summarize(samples), "samples": samples}
        if (
            len(samples) != WARMUPS + MEASURED
            or samples[-1]["error_type"]
            or samples[-1]["owned_outstanding_after_settle"] != 0
        ):
            break
    return results


def _pool_sample(pool: Any) -> dict[str, int]:
    return {
        "idle": len(pool.conns),
        "active_sockets": int(pool.active_sockets),
        "requests": int(pool.requests),
    }


def _redis_snapshot(pool: Any) -> dict[str, int]:
    return {
        "in_use": len(pool._in_use_connections),
        "connected_sockets": sum(
            bool(connection.is_connected)
            for connection in (*pool._in_use_connections, *pool._available_connections)
        ),
    }


def _os_snapshot(session: Any, pool: Any) -> dict[str, Any]:
    return {
        "session_closed": bool(session.closed),
        "pool_closed": bool(pool.closed),
        "acquired": len(pool._acquired),
        "idle": sum(len(group) for group in pool._conns.values()),
    }


def _wv_snapshot(http: Any, grpc: Any) -> dict[str, Any]:
    return {
        "http_closed": bool(http.is_closed),
        "http_connections": len(http._transport._pool.connections),
        "grpc_state": grpc.get_state().name,
    }


def engine_native_probe(
    engine: Any, row: str
) -> tuple[Callable[[], dict[str, Any]], Callable[[dict[str, Any]], int]]:
    """Retain only this Engine's native handles across stop for post-close inspection."""
    if row == "postgres_records":
        pool = engine._sql.engine.sync_engine.pool
        return (
            lambda: {"checked_out": pool.checkedout(), "checked_in": pool.checkedin()},
            lambda sample: sample["checked_out"] + sample["checked_in"],
        )
    if row in {"redis_cache", "valkey_cache"}:
        backend = next(iter(engine._cache._client.cache._backends.values()))
        pool = backend._client.connection_pool
        return lambda: _redis_snapshot(pool), lambda sample: sum(sample.values())
    if row == "mongo_records":
        connector = engine._storage._connector
        async_pool = next(iter(connector.handle._topology._servers.values())).pool
        sync_client = connector._sync
        if sync_client is None:
            raise RuntimeError("Mongo Engine synchronous pool was not exercised")
        sync_pool = next(iter(sync_client._topology._servers.values())).pool

        def mongo_snapshot() -> dict[str, Any]:
            return {"async_pool": _pool_sample(async_pool), "sync_pool": _pool_sample(sync_pool)}

        return (
            mongo_snapshot,
            lambda sample: sum(sample["async_pool"].values()) + sum(sample["sync_pool"].values()),
        )
    if row == "neo4j_graph":
        pool = engine._graph._ex._driver._pool
        return (
            lambda: {"pooled_sockets": sum(len(group) for group in pool.connections.values())},
            lambda sample: sample["pooled_sockets"],
        )
    if row == "opensearch_lexical":
        connections = engine._lexical._client.handle.transport.connection_pool.connections
        if len(connections) != 1 or connections[0].session is None:
            raise RuntimeError("OpenSearch Engine native session telemetry unavailable")
        session = connections[0].session
        pool = session.connector
        if pool is None:
            raise RuntimeError("OpenSearch Engine native pool telemetry unavailable")
        return (
            lambda: _os_snapshot(session, pool),
            lambda sample: (
                sample["acquired"]
                + sample["idle"]
                + int(not sample["session_closed"])
                + int(not sample["pool_closed"])
            ),
        )
    if row == "weaviate_vector_lexical":
        vector_connector = engine._vector._connector
        lexical_connector = engine._lexical._connector
        if vector_connector is not lexical_connector:
            raise RuntimeError("Weaviate vector/lexical slots did not share the connector")
        native = vector_connector.handle._connection
        http, grpc = native._client, native._grpc_channel
        if http is None or grpc is None or not hasattr(http._transport, "_pool"):
            raise RuntimeError("Weaviate Engine native HTTP/gRPC telemetry unavailable")
        return (
            lambda: _wv_snapshot(http, grpc),
            lambda sample: (
                sample["http_connections"]
                + int(not sample["http_closed"])
                + int(sample["grpc_state"] != "SHUTDOWN")
            ),
        )
    raise RuntimeError("unknown Engine row")


def _own_database_name(run_id: str, kind: str) -> str:
    return f"rm31_{kind}_{run_id}"


def _postgres_database(port: int, password: str, database: str, *, create: bool) -> None:
    import psycopg

    if not database.startswith("rm31_pg_") or not database.removeprefix("rm31_pg_").isalnum():
        raise RuntimeError("refusing non-owned PostgreSQL database")
    with psycopg.connect(
        f"postgresql://memspine_local:{password}@127.0.0.1:{port}/postgres",
        autocommit=True,
        connect_timeout=3,
    ) as connection:
        connection.execute(f"{'CREATE' if create else 'DROP'} DATABASE {database}")


def _mongo_drop_database(port: int, database: str) -> None:
    from pymongo import MongoClient

    if not database.startswith("rm31_mg_") or not database.removeprefix("rm31_mg_").isalnum():
        raise RuntimeError("refusing non-owned MongoDB database")
    observer: Any = MongoClient(
        f"mongodb://127.0.0.1:{port}/?directConnection=true", serverSelectionTimeoutMS=3000
    )
    try:
        observer.drop_database(database)
    finally:
        observer.close()


async def _neo4j_drop_owned_schema(label: str) -> None:
    from neo4j import AsyncGraphDatabase

    if not re.fullmatch(r"MemNode_[0-9a-f]{24}", label):
        raise RuntimeError("refusing non-owned Neo4j label")
    driver = AsyncGraphDatabase.driver(
        os.environ["RM31_NEO4J_URL"],
        auth=(os.environ["RM31_NEO4J_USER"], os.environ["RM31_NEO4J_PASSWORD"]),
        connection_timeout=5,
    )
    try:
        async with asyncio.timeout(15):
            async with driver.session(database="neo4j") as session:
                await (await session.run(f"MATCH (n:{label}) DETACH DELETE n")).consume()
                await (await session.run(f"DROP INDEX {label}_id IF EXISTS")).consume()
                await (await session.run(f"DROP CONSTRAINT {label}_key IF EXISTS")).consume()
    finally:
        await driver.close()


def _engine_config(
    row: str,
    data: dict[str, object],
    isolated: dict[str, Any],
    run_id: str,
    cycle: int,
    work_dir: Path,
) -> dict[str, Any]:
    ports = ports_for(data)
    # One durable remote records database owns one deployment binding across lifecycles.
    deployment = (
        f"rm31-{run_id}-{row}"
        if row in {"postgres_records", "mongo_records"}
        else f"rm31-{run_id}-{row}-{cycle}"
    )
    data_dir = (
        work_dir / f"{row}-data"
        if row in {"postgres_records", "mongo_records"}
        else work_dir / f"{row}-{cycle}-data"
    )
    sqlite = {
        "provider": "sqlite",
        "settings": {"path": str(work_dir / f"{row}-{cycle}.sqlite"), "busy_timeout_s": 5},
    }
    storage: dict[str, Any] = {"records": sqlite, "cache": {"provider": "memory", "settings": {}}}
    memories: dict[str, Any] = {"semantic": {"enabled": True}}
    if row == "postgres_records":
        database = _own_database_name(run_id, "pg")
        storage["records"] = {
            "provider": "postgres",
            "settings": {
                "url": f"postgresql://memspine_local:{data['password']}@127.0.0.1:{ports['POSTGRES']}/{database}?application_name=rm31-{run_id}",
                "connect_timeout_s": 5,
                "statement_timeout_s": 10,
            },
        }
    elif row in {"redis_cache", "valkey_cache"}:
        provider = row.split("_")[0]
        storage["cache"] = {
            "provider": provider,
            "settings": {
                "url": f"redis://127.0.0.1:{ports[provider.upper()]}/0?client_name=rm31-{provider}-{run_id}",
                "namespace": f"rm31-{run_id}-{cycle}",
                "connect_timeout_s": 5,
                "request_timeout_s": 5,
            },
        }
    elif row == "mongo_records":
        storage["records"] = {
            "provider": "mongodb",
            "settings": {
                "url": f"mongodb://127.0.0.1:{ports['MONGO']}/?directConnection=true&appName=rm31-{run_id}",
                "database": _own_database_name(run_id, "mg"),
                "connect_timeout_s": 5,
                "server_selection_timeout_s": 5,
            },
        }
    elif row == "neo4j_graph":
        storage["graph"] = {
            "provider": "neo4j",
            "settings": {
                "url_env": "RM31_NEO4J_URL",
                "user_env": "RM31_NEO4J_USER",
                "password_env": "RM31_NEO4J_PASSWORD",
                "connect_timeout_s": 5,
                "request_timeout_s": 5,
            },
        }
        memories["associative"] = {"enabled": True}
    elif row == "opensearch_lexical":
        storage["lexical"] = {
            "provider": "opensearch",
            "settings": {
                "url_env": "RM31_OPENSEARCH_URL",
                "index_prefix": f"rm31{run_id}",
                "connect_timeout_s": 10,
                "request_timeout_s": 10,
            },
        }
    elif row == "weaviate_vector_lexical":
        settings = {
            "url_env": "RM31_WEAVIATE_URL",
            "grpc_port": isolated["weaviate"]["ports"]["50051"],
            "collection_prefix": f"Rm31{run_id}",
            "connect_timeout_s": 10,
            "request_timeout_s": 10,
        }
        storage["vector"] = {"provider": "weaviate", "settings": settings}
        storage["lexical"] = {"provider": "weaviate", "settings": settings}
    return {
        "template": "core",
        "dotenv_path": None,
        "deployment": {"id": deployment, "data_dir": str(data_dir)},
        "storage": storage,
        "model": {"embedding": {"provider": "hash", "settings": {"dim": 64}}},
        "memories": memories,
    }


async def engine_cycle(
    row: str,
    data: dict[str, object],
    isolated: dict[str, Any],
    run_id: str,
    cycle: int,
    work_dir: Path,
) -> dict[str, Any]:
    from memspine import Engine

    engine = Engine(**_engine_config(row, data, isolated, run_id, cycle, work_dir))
    probe: Callable[[], dict[str, Any]] | None = None
    outstanding: Callable[[dict[str, Any]], int] | None = None
    before: dict[str, Any] | None = None
    error: str | None = None
    neo4j_label: str | None = None
    namespace = f"rm31-{run_id}-{row}-{cycle}"
    content = f"RM31 owned {run_id} {row} cycle {cycle} cobalt retrieval"
    try:
        await engine.start()
        if row == "neo4j_graph":
            neo4j_label = str(cast(Any, engine._graph)._ex._fmt["N"])
        record = await engine.write(content, namespace=namespace)
        assert record is not None
        hits = await engine.search("cobalt retrieval", namespace=namespace, top_k=5)
        assert any(item.content == content for item, _score in hits)
        if row in {"redis_cache", "valkey_cache"}:
            assert engine._cache is not None
            await engine._cache.set("rm31-probe", b"owned")
            assert await engine._cache.get("rm31-probe") == b"owned"
            await engine._cache.delete("rm31-probe")
            await cast(Any, engine._cache).delete_prefix("")
        if row == "mongo_records":
            connector = cast(Any, engine._storage)._connector
            await asyncio.to_thread(lambda: connector.sync_database().client.admin.command("ping"))
        if row == "neo4j_graph":
            assert engine._graph is not None
            node = f"rm31-{run_id}-{cycle}"
            await engine._graph.upsert_node(node, namespace=namespace)
            await engine._graph.delete_node(node)
        if row == "opensearch_lexical":
            lexical = cast(Any, engine._lexical)
            await lexical._client.handle.indices.delete(index=lexical._index, ignore=[404])
        if row == "weaviate_vector_lexical":
            vector = cast(Any, engine._vector)
            lexical = cast(Any, engine._lexical)
            native = vector._connector.handle
            await vector._collection.query.fetch_objects(limit=1)
            for name in (vector._binding.name, lexical._binding.name):
                if await native.collections.exists(name):
                    await native.collections.delete(name)
        probe, outstanding = engine_native_probe(engine, row)
        before = probe()
        if row == "weaviate_vector_lexical" and before["grpc_state"] != "READY":
            raise RuntimeError("Weaviate Engine gRPC channel was not READY after query")
    except BaseException as exc:
        error = type(exc).__name__
    try:
        await engine.stop()
    except BaseException as exc:
        error = error or f"stop:{type(exc).__name__}"
    states = engine._provider_resources.states if engine._provider_resources is not None else {}
    if row == "neo4j_graph" and neo4j_label is None and engine._graph is not None:
        neo4j_label = str(cast(Any, engine._graph)._ex._fmt["N"])
    stopped = bool(states) and all(state.value == "stopped" for state in states.values())
    after: dict[str, Any] | None = None
    owned_outstanding: int | None = None
    try:
        after = probe() if probe is not None else None
        owned_outstanding = (
            outstanding(after) if after is not None and outstanding is not None else None
        )
    except BaseException as exc:
        error = error or f"telemetry_after_stop:{type(exc).__name__}"
        after = None
        owned_outstanding = None
    scratch_cleanup: dict[str, Any] = {}
    if neo4j_label is not None:
        try:
            await _neo4j_drop_owned_schema(neo4j_label)
            scratch_cleanup["neo4j_schema"] = "dropped"
        except BaseException as exc:
            scratch_cleanup["neo4j_schema_error_type"] = type(exc).__name__
            error = error or f"scratch_cleanup:{type(exc).__name__}"
    return {
        "error_type": error,
        "telemetry_available": after is not None,
        "telemetry_before_close": before,
        "telemetry_after_close": after,
        "owned_outstanding_after_settle": owned_outstanding,
        "resource_states": {name: state.value for name, state in states.items()},
        "all_resources_stopped": stopped,
        "scratch_cleanup": scratch_cleanup,
    }


async def run_engine_matrix(
    data: dict[str, object], isolated: dict[str, Any], deadline: float, run_id: str, work_dir: Path
) -> dict[str, Any]:
    ports = ports_for(data)
    pg_db = _own_database_name(run_id, "pg")
    mongo_db = _own_database_name(run_id, "mg")
    results: dict[str, Any] = {}
    cleanup: dict[str, Any] = {}
    await asyncio.to_thread(
        _postgres_database, ports["POSTGRES"], str(data["password"]), pg_db, create=True
    )
    try:
        for row in ENGINE_ROWS:
            samples: list[dict[str, Any]] = []
            for cycle in range(WARMUPS + MEASURED):
                started = time.monotonic()
                sample: dict[str, Any] = {
                    "cycle": cycle,
                    "phase": "warmup" if cycle < WARMUPS else "measured",
                    "rss_before": rss_bytes(),
                    "handles_before": handle_count(),
                    "error_type": None,
                    "telemetry_available": False,
                    "owned_outstanding_after_settle": None,
                    "all_resources_stopped": False,
                }
                try:
                    if started >= deadline:
                        raise TimeoutError("global wall cap reached")
                    async with asyncio.timeout(min(60.0, deadline - started)):
                        sample.update(
                            await engine_cycle(row, data, isolated, run_id, cycle, work_dir)
                        )
                except BaseException as exc:
                    sample["error_type"] = type(exc).__name__
                sample["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
                sample["rss_after_stop"] = rss_bytes()
                sample["handles_after_stop"] = handle_count()
                samples.append(sample)
                if (
                    sample["error_type"]
                    or sample["owned_outstanding_after_settle"] != 0
                    or not sample["all_resources_stopped"]
                ):
                    break
            results[row] = {"summary": summarize(samples), "samples": samples}
            if len(samples) != WARMUPS + MEASURED:
                break
    finally:
        try:
            await asyncio.to_thread(
                _postgres_database, ports["POSTGRES"], str(data["password"]), pg_db, create=False
            )
            cleanup["postgres_database"] = "dropped"
        except BaseException as exc:
            cleanup["postgres_database_error_type"] = type(exc).__name__
        try:
            await asyncio.to_thread(_mongo_drop_database, ports["MONGO"], mongo_db)
            cleanup["mongo_database"] = "dropped"
        except BaseException as exc:
            cleanup["mongo_database_error_type"] = type(exc).__name__
    return {"rows": results, "scratch_cleanup": cleanup}


async def weaviate_two_engine_overlap(
    data: dict[str, object], isolated: dict[str, Any], run_id: str, work_dir: Path
) -> dict[str, Any]:
    from memspine import Engine

    first = Engine(
        **_engine_config("weaviate_vector_lexical", data, isolated, run_id, 10000, work_dir)
    )
    second = Engine(
        **_engine_config("weaviate_vector_lexical", data, isolated, run_id, 10001, work_dir)
    )
    first_started = second_started = False
    error: str | None = None
    shared = False
    after_first: dict[str, Any] | None = None
    after_second: dict[str, Any] | None = None
    try:
        await first.start()
        first_started = True
        await second.start()
        second_started = True
        first_vector = cast(Any, first._vector)
        first_lexical = cast(Any, first._lexical)
        second_vector = cast(Any, second._vector)
        second_lexical = cast(Any, second._lexical)
        shared = (
            first_vector._connector is first_lexical._connector
            and first_vector._connector is second_vector._connector
            and second_vector._connector is second_lexical._connector
        )
        if not shared:
            raise RuntimeError("Weaviate two-Engine native connector was not shared")
        native = first_vector._connector.handle._connection
        http, grpc = native._client, native._grpc_channel
        if http is None or grpc is None:
            raise RuntimeError("Weaviate shared native transports unavailable")
        await first.stop()
        first_started = False
        after_first = _wv_snapshot(http, grpc)
        if after_first["http_closed"] or after_first["grpc_state"] == "SHUTDOWN":
            raise RuntimeError("stopping first Engine closed peer's Weaviate transport")
        namespace = f"rm31-{run_id}-overlap"
        content = f"RM31 shared Weaviate {run_id} cobalt retrieval"
        await second.write(content, namespace=namespace)
        hits = await second.search("cobalt retrieval", namespace=namespace, top_k=5)
        if not any(item.content == content for item, _score in hits):
            raise RuntimeError("second Engine became unusable after first stopped")
        await second_vector._collection.query.fetch_objects(limit=1)
        for name in (
            first_vector._binding.name,
            first_lexical._binding.name,
            second_vector._binding.name,
            second_lexical._binding.name,
        ):
            if await second_vector._connector.handle.collections.exists(name):
                await second_vector._connector.handle.collections.delete(name)
        await second.stop()
        second_started = False
        after_second = _wv_snapshot(http, grpc)
    except BaseException as exc:
        error = type(exc).__name__
    finally:
        if first_started:
            try:
                await first.stop()
            except BaseException as exc:
                error = error or f"first_stop:{type(exc).__name__}"
        if second_started:
            try:
                await second.stop()
            except BaseException as exc:
                error = error or f"second_stop:{type(exc).__name__}"
    complete = (
        error is None
        and shared
        and after_first is not None
        and after_second is not None
        and after_first["http_closed"] is False
        and after_first["grpc_state"] != "SHUTDOWN"
        and after_second["http_closed"] is True
        and after_second["http_connections"] == 0
        and after_second["grpc_state"] == "SHUTDOWN"
    )
    return {
        "passed": complete,
        "error_type": error,
        "shared_connector": shared,
        "after_first_stop": after_first,
        "after_second_stop": after_second,
    }


def source_state() -> dict[str, Any]:
    sources = sorted((ROOT / "src/memspine").rglob("*.py")) + [
        ROOT / "scripts/check_remote_provider_resource_soak.py",
        ROOT / "scripts/rm31_owned_services.py",
        ROOT / "scripts/local_services.py",
    ]
    aggregate = hashlib.sha256()
    for path in sources:
        aggregate.update(str(path.relative_to(ROOT)).replace("\\", "/").encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(hashlib.sha256(path.read_bytes()).digest())
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=False
    ).stdout.strip()
    return {
        "revision": revision,
        "dirty": bool(dirty),
        "runtime_tree_sha256": aggregate.hexdigest(),
        "runtime_tree_file_count": len(sources),
        "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_file_sha256": {
            str(path.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
            for path in (
                ROOT / "scripts/local_services.py",
                ROOT / "scripts/rm31_owned_services.py",
                ROOT / "src/memspine/clients/postgres/connector.py",
                ROOT / "src/memspine/clients/cashews.py",
                ROOT / "src/memspine/clients/mongodb/connector.py",
                ROOT / "src/memspine/clients/neo4j.py",
                ROOT / "src/memspine/clients/opensearch.py",
                ROOT / "src/memspine/clients/weaviate.py",
                ROOT / "src/memspine/api/engine.py",
                ROOT / "src/memspine/runtime/container.py",
                ROOT / "src/memspine/providers/builtin/cache.py",
                ROOT / "src/memspine/providers/builtin/graph.py",
                ROOT / "src/memspine/providers/builtin/mongodb.py",
                ROOT / "src/memspine/providers/builtin/opensearch.py",
                ROOT / "src/memspine/providers/builtin/weaviate.py",
                ROOT / "src/memspine/providers/connectors.py",
                ROOT / "src/memspine/providers/lifecycle.py",
                ROOT / "src/memspine/providers/shared.py",
                ROOT / "src/memspine/config/schema/providers.py",
                ROOT / "src/memspine/config/schema/remote_providers.py",
                ROOT / "src/memspine/services/cache/cashews.py",
                ROOT / "src/memspine/services/graph/neo4j.py",
                ROOT / "src/memspine/services/lexical/opensearch.py",
                ROOT / "src/memspine/services/lexical/weaviate_store.py",
                ROOT / "src/memspine/services/vector/weaviate_store.py",
                ROOT / "src/memspine/services/storage/mongodb.py",
                ROOT / "src/memspine/services/storage/postgres/engine.py",
            )
        },
    }


def image_metadata(data: dict[str, object]) -> dict[str, str]:
    metadata: dict[str, str] = {}
    for container_id in owned_containers(data):
        result = run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}|{{.State.Status}}|{{.Image}}',
                container_id,
            ]
        )
        service, status, image_id = result.stdout.strip().split("|", 2)
        if status != "running":
            raise RuntimeError(f"owned {service} service is not running")
        if service in BASE_SERVICES:
            metadata[service] = image_id
    if set(metadata) != set(BASE_SERVICES):
        raise RuntimeError("owned data-service inventory incomplete")
    return metadata


def native_versions(data: dict[str, object]) -> dict[str, Any]:
    """Read versions through separate docker exec processes, outside measured clients."""
    commands = {
        "postgres": [
            "psql",
            "-U",
            "memspine_local",
            "-d",
            "memspine_local",
            "-Atc",
            "SHOW server_version",
        ],
        "redis": ["redis-server", "--version"],
        "valkey": ["valkey-server", "--version"],
        "mongo": ["mongod", "--version"],
        "neo4j": ["neo4j", "--version"],
    }
    versions: dict[str, Any] = {
        "observer": "separate docker exec processes; not measured client pools"
    }
    for container_id in owned_containers(data):
        service = run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.service"}}',
                container_id,
            ]
        ).stdout.strip()
        if service not in commands:
            continue
        result = run(["docker", "exec", container_id, *commands[service]], check=False)
        # Version outputs contain no credentials; retain one bounded line only.
        versions[service] = (
            (result.stdout.strip().splitlines() or ["unavailable"])[0][:160]
            if result.returncode == 0
            else "unavailable"
        )
    versions["drivers"] = {
        name: importlib.metadata.version(name)
        for name in (
            "SQLAlchemy",
            "psycopg",
            "redis",
            "pymongo",
            "neo4j",
            "opensearch-py",
            "weaviate-client",
            "aiohttp",
            "httpx",
            "grpcio",
        )
    }
    return versions


def isolated_versions(isolated: dict[str, Any]) -> dict[str, Any]:
    """Separate short-lived observer HTTP connections, outside measured pools."""
    versions: dict[str, Any] = {"observer": "separate urllib connection"}
    for service, internal, path in (
        ("opensearch", "9200", "/"),
        ("weaviate", "8080", "/v1/meta"),
    ):
        port = isolated[service]["ports"][internal]
        with urlopen(f"http://127.0.0.1:{port}{path}", timeout=5) as response:
            payload = json.load(response)
        version = payload.get("version") or payload.get("Version")
        versions[service] = version if isinstance(version, (str, dict)) else "unavailable"
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--owned-state", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.out.exists():
        parser.error("RM-31 output path already exists")
    data = load_state()
    if data is None:
        parser.error("owned RM-136 stack state is absent")
    if args.worker:
        owned_state = (
            isolated_services.load(args.owned_state)
            if args.owned_state is not None
            else isolated_services.new_state()
        )
        if args.owned_state is None:
            isolated_services.save(owned_state)
        # Keep the OS lock in the measuring process through the final artifact write.
        with exclusive_run():
            return _run_locked(data, args.out, owned_state)
    # subprocess.run kills and waits for this owned child on timeout. The worker holds
    # the lock, so parent death cannot release it while measurement continues.
    try:
        owned_state = isolated_services.new_state()
        isolated_services.save(owned_state)
        owned_path = isolated_services.state_path(owned_state)
        child_env = os.environ.copy()
        ports = ports_for(data)
        child_env.update(
            {
                "RM31_NEO4J_URL": f"bolt://127.0.0.1:{ports['NEO4J']}",
                "RM31_NEO4J_USER": "neo4j",
                "RM31_NEO4J_PASSWORD": str(data["password"]),
            }
        )
        child = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--out",
                str(args.out),
                "--owned-state",
                str(owned_path),
            ],
            cwd=ROOT,
            capture_output=True,
            text=True,
            env=child_env,
            timeout=WALL_CAP_S,
            check=False,
        )
        if args.out.exists():
            result = json.loads(args.out.read_text(encoding="utf-8"))
            print(
                json.dumps(
                    {
                        "run_id": result.get("run_id"),
                        "passed_connector_subset": result.get("passed_connector_subset"),
                        "full_gate_passed": result.get("full_gate_passed"),
                        "source_unchanged_during_run": result.get("source_unchanged_during_run"),
                        "wall_s": result.get("wall_s"),
                    }
                )
            )
        else:
            print("RM-31 worker exited before writing a report", file=sys.stderr)
        if owned_path.exists():
            isolated_services.cleanup(isolated_services.load(owned_path))
        return child.returncode
    except subprocess.TimeoutExpired:
        cleanup = (
            isolated_services.cleanup(isolated_services.load(owned_path))
            if owned_path.exists()
            else {}
        )
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "timed_out": True,
                    "wall_cap_s": WALL_CAP_S,
                    "source": source_state(),
                    "isolated_cleanup": cleanup,
                    "credentials_recorded": False,
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print("RM-31 worker exceeded hard wall cap", file=sys.stderr)
        return 2


def _run_locked(data: dict[str, object], out: Path, owned_state: dict[str, Any]) -> int:
    image_ids = image_metadata(data)
    run_id = uuid.uuid4().hex[:12]
    report: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "created_at_utc": datetime.now(UTC).isoformat(),
        "declared_before_run": {
            "warmups_per_provider": WARMUPS,
            "measured_cycles_per_provider": MEASURED,
            "wall_cap_s": WALL_CAP_S,
            "per_cycle_timeout_s": 15,
            "close_settle_s": SETTLE_S,
            "no_overlapping_runs": True,
        },
        "scope": {
            "included": list(SERVICES),
            "excluded": {"temporal": "not a data provider pool"},
            "cycle_owner": "Memspine connector and Engine-owned native pools in separate phases",
        },
        "image_ids": image_ids,
        "native_versions": native_versions(data),
        "source": source_state(),
        "credentials_recorded": False,
        "rss_handle_interpretation": "diagnostic slopes only; finite run does not prove unbounded leak freedom",
    }
    started = time.monotonic()
    saved_env = {
        "RM31_OPENSEARCH_URL": os.environ.get("RM31_OPENSEARCH_URL"),
        "RM31_WEAVIATE_URL": os.environ.get("RM31_WEAVIATE_URL"),
    }
    try:
        isolated = {
            service: isolated_services.start(owned_state, service, deadline=started + 180)
            for service in ("opensearch", "weaviate")
        }
        report["isolated_services"] = isolated
        report["isolated_versions"] = isolated_versions(isolated)
        os.environ["RM31_OPENSEARCH_URL"] = (
            f"http://127.0.0.1:{isolated['opensearch']['ports']['9200']}"
        )
        os.environ["RM31_WEAVIATE_URL"] = (
            f"http://127.0.0.1:{isolated['weaviate']['ports']['8080']}"
        )
        # psycopg async requires SelectorEventLoop on Windows (the default is Proactor).
        loop_factory = asyncio.SelectorEventLoop if sys.platform == "win32" else None
        report["providers"] = asyncio.run(
            run_cycles(data, isolated, started + WALL_CAP_S, run_id), loop_factory=loop_factory
        )
        connectors = report["providers"]
        if len(connectors) == len(SERVICES) and all(
            item["summary"]["warmups_completed"] == WARMUPS
            and item["summary"]["measured_completed"] == MEASURED
            and item["summary"]["owned_outstanding_zero_after_settle"] is True
            for item in connectors.values()
        ):
            with tempfile.TemporaryDirectory(prefix=f"memspine-rm31-{run_id}-") as directory:
                work_dir = Path(directory)
                report["engine_matrix"] = asyncio.run(
                    run_engine_matrix(data, isolated, started + WALL_CAP_S, run_id, work_dir),
                    loop_factory=loop_factory,
                )
                report["weaviate_two_engine_overlap"] = asyncio.run(
                    weaviate_two_engine_overlap(data, isolated, run_id, work_dir),
                    loop_factory=loop_factory,
                )
    except BaseException as exc:
        report["fatal_error_type"] = type(exc).__name__
    finally:
        for key, value in saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        try:
            report["isolated_cleanup"] = isolated_services.cleanup(owned_state)
        except BaseException as exc:
            report["isolated_cleanup_error_type"] = type(exc).__name__
    report["wall_s"] = round(time.monotonic() - started, 3)
    report["source_after"] = source_state()
    report["source_unchanged_during_run"] = (
        report["source"]["revision"] == report["source_after"]["revision"]
        and report["source"]["harness_sha256"] == report["source_after"]["harness_sha256"]
        and report["source"]["runtime_tree_sha256"] == report["source_after"]["runtime_tree_sha256"]
        and report["source"]["runtime_tree_file_count"]
        == report["source_after"]["runtime_tree_file_count"]
        and report["source"]["source_file_sha256"] == report["source_after"]["source_file_sha256"]
    )
    providers = report.get("providers", {})
    report["passed_connector_subset"] = (
        report["source_unchanged_during_run"]
        and "isolated_cleanup_error_type" not in report
        and len(providers) == len(SERVICES)
        and all(
            p["summary"]["warmups_completed"] == WARMUPS
            and p["summary"]["measured_completed"] == MEASURED
            and p["summary"]["owned_outstanding_zero_after_settle"] is True
            for p in providers.values()
        )
    )
    matrix = report.get("engine_matrix", {})
    rows = matrix.get("rows", {})
    scratch_cleanup = matrix.get("scratch_cleanup", {})
    isolated_cleanup = report.get("isolated_cleanup", {})
    report["full_gate_passed"] = (
        report["passed_connector_subset"]
        and len(rows) == len(ENGINE_ROWS)
        and all(
            item["summary"]["warmups_completed"] == WARMUPS
            and item["summary"]["measured_completed"] == MEASURED
            and item["summary"]["owned_outstanding_zero_after_settle"] is True
            and all(sample.get("all_resources_stopped") is True for sample in item["samples"])
            for item in rows.values()
        )
        and scratch_cleanup.get("postgres_database") == "dropped"
        and scratch_cleanup.get("mongo_database") == "dropped"
        and report.get("weaviate_two_engine_overlap", {}).get("passed") is True
        and isolated_cleanup.get("opensearch") == "removed"
        and isolated_cleanup.get("weaviate") == "removed"
        and report.get("fatal_error_type") is None
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    temporary = out.with_suffix(out.suffix + ".new")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(out)
    print(
        json.dumps(
            {
                "run_id": run_id,
                "passed_connector_subset": report["passed_connector_subset"],
                "full_gate_passed": report["full_gate_passed"],
                "wall_s": report["wall_s"],
                "provider_summaries": {k: v["summary"] for k, v in providers.items()},
                "engine_summaries": {k: v["summary"] for k, v in rows.items()},
            },
            indent=2,
        )
    )
    return 0 if report["full_gate_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
