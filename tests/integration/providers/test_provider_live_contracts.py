"""Live provider contracts against disposable embedded databases or opted-in services.

Run with ``python scripts/run_provider_live_contracts.py``. Remote tests use only
the explicitly named ``MEMSPINE_TEST_*`` environment variables below and skip
with the missing variable names when no local service is configured.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from memspine import Engine
from memspine.providers.health import ProbeStatus
from memspine.providers.lifecycle import LifecycleState


def _deployment() -> dict[str, str]:
    return {"id": f"provider-live-{uuid.uuid4().hex}"}


def _sqlite(path: Path) -> dict[str, Any]:
    return {
        "provider": "sqlite",
        "settings": {"path": str(path), "busy_timeout_s": 5.0},
    }


async def _assert_healthy(engine: Engine, *expected: tuple[str, str]) -> None:
    report = await engine.provider_health()
    for category, provider in expected:
        matches = [
            result
            for result in report.providers
            if result.category == category and result.provider == provider
        ]
        assert matches, f"missing health result for {category}.{provider}"
        assert all(result.status is ProbeStatus.HEALTHY for result in matches), matches


async def _close_and_assert_owned_resources_stopped(engine: Engine) -> None:
    await engine.stop()
    manager = engine._provider_resources
    assert manager is not None
    assert manager.states
    assert all(state is LifecycleState.STOPPED for state in manager.states.values())


async def _assert_provider_bindings(
    engine: Engine, deployment_id: str
) -> tuple[tuple[str, str, str], ...]:
    assert engine._sql is not None
    async with engine._sql.engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT projector_name, provider, target_fingerprint "
                "FROM provider_projector_bindings WHERE deployment_id = :deployment_id "
                "ORDER BY projector_name"
            ),
            {"deployment_id": deployment_id},
        )
        bindings = tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows)
    assert bindings
    assert all(fingerprint for _, _, fingerprint in bindings)
    return bindings


@pytest.mark.asyncio
@pytest.mark.provider_live
async def test_embedded_storage_vector_lexical_graph_and_cache_contracts(
    tmp_path: Path,
) -> None:
    """Exercise real writes, projections, restart search and native cleanup."""
    deployment_id = f"provider-live-{uuid.uuid4().hex}"

    def build_engine() -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            deployment={"id": deployment_id},
            storage={
                "records": _sqlite(tmp_path / "events.sqlite"),
                "vector": {"provider": "lancedb", "settings": {"isolation": "shared"}},
                "cache": {
                    "provider": "disk",
                    "settings": {"path": str(tmp_path / "cache"), "namespace": "live-contract"},
                },
                "graph": {"provider": "ladybug", "settings": {}},
                "lexical": {"provider": "tantivy", "settings": {}},
            },
            embedding={"provider": "hash", "settings": {"dim": 64}},
            memories={"semantic": {"enabled": True}, "associative": {"enabled": True}},
        )

    engine = build_engine()
    content = f"live contract record {uuid.uuid4().hex} describes cobalt observability"
    await engine.start()
    try:
        await _assert_healthy(
            engine,
            ("storage", "sqlite"),
            ("vector", "lancedb"),
            ("lexical", "tantivy"),
            ("graph", "ladybug"),
            ("cache", "disk"),
        )
        assert engine._sql is not None
        assert await engine.write(content, namespace="live-contract")
        bindings_before = await _assert_provider_bindings(engine, deployment_id)
        found = await engine.search("cobalt observability", namespace="live-contract", top_k=5)
        assert any(record.content == content for record, _ in found)
        assert engine._graph is not None
        graph_namespace = f"live-contract-{uuid.uuid4().hex}"
        await engine._graph.upsert_node("source", namespace=graph_namespace)
        await engine._graph.upsert_node("target", namespace=graph_namespace)
        await engine._graph.upsert_node("other-target", namespace=f"{graph_namespace}-other")
        await engine._graph.upsert_edge("source", "target", "related", namespace=graph_namespace)
        await engine._graph.upsert_edge(
            "source", "other-target", "related", namespace=f"{graph_namespace}-other"
        )
        assert [
            n.node_id for n in await engine._graph.neighbors("source", namespace=graph_namespace)
        ] == ["target"]
        assert len(await engine._graph.edge_list(namespace=graph_namespace)) == 1
        await engine._graph.delete_node("source")
        assert await engine._graph.edge_list(namespace=graph_namespace) == []
        assert engine._cache is not None
        await engine._cache.set("contract-probe", b"live")
        assert await engine._cache.get("contract-probe") == b"live"
    finally:
        await _close_and_assert_owned_resources_stopped(engine)

    reopened = build_engine()
    await reopened.start()
    try:
        bindings_after = await _assert_provider_bindings(reopened, deployment_id)
        assert bindings_after == bindings_before
        found = await reopened.search("cobalt observability", namespace="live-contract", top_k=5)
        assert any(record.content == content for record, _ in found)
    finally:
        await _close_and_assert_owned_resources_stopped(reopened)


def _require_env(*names: str) -> dict[str, str]:
    absent = [name for name in names if not os.environ.get(name)]
    if absent:
        pytest.skip(
            f"local service not configured; missing environment variable names: {', '.join(absent)}"
        )
    return {name: os.environ[name] for name in names}


@pytest.mark.asyncio
@pytest.mark.provider_live
async def test_postgres_storage_contract(tmp_path: Path) -> None:
    env = _require_env("MEMSPINE_TEST_LIVE_POSTGRES_URL")
    deployment_id = f"provider-live-{uuid.uuid4().hex}"

    def build_engine() -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            deployment={"data_dir": str(tmp_path / "derived"), "id": deployment_id},
            storage={
                "records": {
                    "provider": "postgres",
                    "settings": {
                        "url": env["MEMSPINE_TEST_LIVE_POSTGRES_URL"],
                        "connect_timeout_s": 10.0,
                        "statement_timeout_s": 10.0,
                    },
                },
                "vector": {"provider": "lancedb", "settings": {"isolation": "shared"}},
                "cache": {"provider": "memory", "settings": {}},
            },
            embedding={"provider": "hash", "settings": {"dim": 64}},
            memories={"semantic": {"enabled": True}},
        )

    engine = build_engine()
    content = f"postgres live contract {uuid.uuid4().hex} stores quartz replication"
    await engine.start()
    try:
        await _assert_healthy(engine, ("storage", "postgres"))
        await engine.write(content, namespace="postgres-live-contract")
        bindings_before = await _assert_provider_bindings(engine, deployment_id)
        found = await engine.search(
            "quartz replication", namespace="postgres-live-contract", top_k=5
        )
        assert any(record.content == content for record, _ in found)
    finally:
        await _close_and_assert_owned_resources_stopped(engine)

    reopened = build_engine()
    await reopened.start()
    try:
        assert await _assert_provider_bindings(reopened, deployment_id) == bindings_before
        found = await reopened.search(
            "quartz replication", namespace="postgres-live-contract", top_k=5
        )
        assert any(record.content == content for record, _ in found)
    finally:
        await _close_and_assert_owned_resources_stopped(reopened)


@pytest.mark.asyncio
@pytest.mark.provider_live
async def test_neo4j_graph_contract() -> None:
    env = _require_env(
        "MEMSPINE_TEST_NEO4J_URL",
        "MEMSPINE_TEST_NEO4J_USER",
        "MEMSPINE_TEST_NEO4J_PASSWORD",
    )
    engine = Engine(
        template="core",
        dotenv_path=None,
        deployment=_deployment(),
        storage={
            "records": _sqlite(Path(":memory:")),
            "cache": {"provider": "memory", "settings": {}},
            "graph": {
                "provider": "neo4j",
                "settings": {
                    "url_env": "MEMSPINE_TEST_NEO4J_URL",
                    "user_env": "MEMSPINE_TEST_NEO4J_USER",
                    "password_env": "MEMSPINE_TEST_NEO4J_PASSWORD",
                    "connect_timeout_s": 10.0,
                    "request_timeout_s": 10.0,
                },
            },
        },
        embedding={"provider": "hash", "settings": {"dim": 64}},
        memories={"semantic": {"enabled": True}, "associative": {"enabled": True}},
    )
    await engine.start()
    try:
        await _assert_healthy(engine, ("graph", "neo4j"))
        assert engine._graph is not None
        namespace = f"live-contract-{uuid.uuid4().hex}"
        other_namespace = f"{namespace}-other"
        source, target, other_target = (
            f"{uuid.uuid4().hex}-{suffix}" for suffix in ("s", "t", "o")
        )
        await engine._graph.upsert_node(source, namespace=namespace)
        await engine._graph.upsert_node(target, namespace=namespace)
        await engine._graph.upsert_node(other_target, namespace=other_namespace)
        await engine._graph.upsert_edge(source, target, "live_related", namespace=namespace)
        await engine._graph.upsert_edge(
            source, other_target, "live_related", namespace=other_namespace
        )
        assert {
            node.node_id for node in await engine._graph.neighbors(source, namespace=namespace)
        } == {target}
        assert len(await engine._graph.edge_list(namespace=namespace)) == 1
        await engine._graph.delete_node(source)
        await engine._graph.delete_node(target)
        await engine._graph.delete_node(other_target)
        assert not await engine._graph.edge_list(namespace=namespace)
        assert not await engine._graph.edge_list(namespace=other_namespace)
    finally:
        await _close_and_assert_owned_resources_stopped(engine)


@pytest.mark.asyncio
@pytest.mark.provider_live
@pytest.mark.parametrize(
    ("provider", "url_env"),
    (
        ("redis", "MEMSPINE_TEST_LIVE_REDIS_URL"),
        ("valkey", "MEMSPINE_TEST_LIVE_VALKEY_URL"),
    ),
)
async def test_remote_cache_contract(provider: str, url_env: str) -> None:
    env = _require_env(url_env)
    deployment_a = f"provider-live-{uuid.uuid4().hex}"
    deployment_b = f"provider-live-{uuid.uuid4().hex}"

    def build_engine(deployment_id: str) -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            deployment={"id": deployment_id},
            storage={
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
                },
                "cache": {
                    "provider": provider,
                    "settings": {
                        "url": env[url_env],
                        "namespace": "provider-live",
                        "default_ttl_seconds": 30.0,
                        "connect_timeout_s": 10.0,
                        "request_timeout_s": 10.0,
                    },
                },
            },
            embedding={"provider": "hash", "settings": {"dim": 64}},
            memories={"semantic": {"enabled": True}},
        )

    engine = build_engine(deployment_a)
    peer = build_engine(deployment_b)
    await engine.start()
    await peer.start()
    try:
        await _assert_healthy(engine, ("cache", provider))
        await _assert_healthy(peer, ("cache", provider))
        assert engine._cache is not None
        assert peer._cache is not None
        key = f"contract-{uuid.uuid4().hex}"
        await engine._cache.set(key, b"live")
        assert await engine._cache.get(key) == b"live"
        assert await peer._cache.get(key) is None
        await engine._cache.set(f"{key}-expires", b"short-lived", ttl_seconds=0.05)
        await asyncio.sleep(0.1)
        assert await engine._cache.get(f"{key}-expires") is None
        await engine._cache.delete(key)
        assert await engine._cache.get(key) is None
    finally:
        await _close_and_assert_owned_resources_stopped(engine)
        await _close_and_assert_owned_resources_stopped(peer)
