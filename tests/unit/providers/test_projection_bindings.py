from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from memspine import Engine
from memspine.exceptions import ConfigError, MemspineError
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.projection_bindings import (
    ProjectionTargetBinding,
    SqlProjectionBindingStore,
)
from memspine.providers.registry import ProviderDescriptor, get_registry
from memspine.services.storage.sqlite.schema import memory_events, metadata


async def test_binding_stays_stable_and_rejects_target_change(tmp_path: Path) -> None:
    assert {
        "provider_change_operations",
        "provider_projection_generations",
        "provider_projector_bindings",
    } <= set(metadata.tables)
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'bindings.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    store = SqlProjectionBindingStore(engine)
    original = ProjectionTargetBinding("deployment-a", "graph", "graph:test-a:v1", "a" * 64)
    generation = await store.ensure(original)

    assert await store.ensure(original) == generation
    with pytest.raises(ConfigError, match="verified projection switch"):
        await store.ensure(
            ProjectionTargetBinding("deployment-a", "graph", "graph:test-b:v1", "b" * 64)
        )
    await engine.dispose()


async def test_legacy_nonzero_offset_without_binding_fails_closed(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
        await connection.execute(
            memory_events.insert().values(
                seq=1,
                event_id="event-1",
                kind="write",
                namespace="default",
                ts="2026-01-01T00:00:00+00:00",
                actor="test",
                schema_version=1,
                payload=b"{}",
                compressed=False,
                fingerprint="f" * 64,
            )
        )
        from memspine.services.storage.sqlite.schema import projector_offsets

        await connection.execute(
            projector_offsets.insert().values(
                projector_name="graph", last_seq=1, updated_at="2026-01-01T00:00:00+00:00"
            )
        )
    store = SqlProjectionBindingStore(engine)

    with pytest.raises(ConfigError, match="legacy offset"):
        await store.ensure(
            ProjectionTargetBinding("deployment-a", "graph", "graph:test-a:v1", "a" * 64)
        )
    await engine.dispose()


async def test_verified_switch_rebinds_only_at_the_verified_checkpoint(tmp_path: Path) -> None:
    from memspine.services.storage.sqlite.schema import (
        projector_offsets,
        provider_projector_bindings,
    )

    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'switch.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(metadata.create_all)
    store = SqlProjectionBindingStore(engine)
    source = ProjectionTargetBinding(
        "deployment-a", "graph", "graph:ladybug:v1", "source-fingerprint", "generation-source"
    )
    assert await store.ensure(source) == "generation-source"
    async with engine.begin() as connection:
        await connection.execute(
            projector_offsets.insert().values(
                projector_name="graph", last_seq=7, updated_at="2026-01-01T00:00:00+00:00"
            )
        )

    target = ProjectionTargetBinding(
        "deployment-a",
        "graph",
        "graph:neo4j:v1",
        "target-fingerprint",
        "generation-target",
        source_provider="ladybug",
        source_target_fingerprint="source-fingerprint",
        source_generation_id="generation-source",
        verified_checkpoint_seq=7,
    )
    wrong_checkpoint = replace(target, verified_checkpoint_seq=6)
    with pytest.raises(ConfigError, match="verified target checkpoint"):
        await store.ensure(wrong_checkpoint)
    assert await store.ensure(target) == "generation-target"
    assert await store.ensure(target) == "generation-target"
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                select(provider_projector_bindings.c.generation_id).where(
                    provider_projector_bindings.c.projector_name == "graph"
                )
            )
        ).one()
    assert row.generation_id == "generation-target"
    await engine.dispose()


class _AliasedBuilder:
    def __init__(self, wrapped: Any, provider_name: str) -> None:
        self._wrapped = wrapped
        self._provider_name = provider_name

    async def build(self, settings: Any, context: Any) -> BuildResult[Any]:
        result = await self._wrapped.build(settings, context)
        return replace(result, provider=ProviderRef("graph", self._provider_name))


def _register_fake_graph(name: str) -> ProviderDescriptor:
    registry = get_registry("graph_stores")
    base = registry.get("sql_adjacency")
    descriptor = ProviderDescriptor(
        ProviderRef("graph", name),
        base.settings_model,
        base.declared_capabilities,
        base.requirements,
        _AliasedBuilder(base.builder, name),
        service_manifest=None,
        requires_deployment_id=False,
    )
    registry.register(name, descriptor, builtin=False)
    return descriptor


def _graph_engine(database: Path, graph_provider: str) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "provider": "sqlite",
            "settings": {"path": str(database), "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {}},
        graph={"provider": graph_provider},
        memories={"associative": {"enabled": True}},
    )


async def test_engine_rejects_graph_provider_swap_before_startup_serves_reads(
    tmp_path: Path,
) -> None:
    registry = get_registry("graph_stores")
    registered: list[str] = []
    for name in ("test_binding_graph_a", "test_binding_graph_b"):
        _register_fake_graph(name)
        registered.append(name)
    database = tmp_path / "events.sqlite"
    try:
        first = _graph_engine(database, "test_binding_graph_a")
        await first.start()
        await first.write("source event survives provider changes", namespace="binding-test")
        await first.stop()

        unchanged = _graph_engine(database, "test_binding_graph_a")
        await unchanged.start()
        try:
            matches = await unchanged.search(
                "source event survives", namespace="binding-test", top_k=1
            )
            assert matches
        finally:
            await unchanged.stop()

        changed = _graph_engine(database, "test_binding_graph_b")
        with pytest.raises(ConfigError, match="different target|selects"):
            await changed.start()
        assert not changed.is_started
        with pytest.raises(MemspineError, match="Engine not started"):
            await changed.list_records(namespace="binding-test")

        check_engine = create_async_engine(f"sqlite+aiosqlite:///{database}")
        async with check_engine.connect() as connection:
            count = await connection.scalar(select(func.count()).select_from(memory_events))
        await check_engine.dispose()
        assert count and count >= 1
    finally:
        for name in registered:
            if name in registry:
                registry.unregister(name)


async def test_engine_rejects_same_primary_mirror_target_before_builder_connects(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memspine.providers.builtin.graph import _neo4j_target_identity
    from memspine.providers.settings import Neo4jSettings

    registry = get_registry("graph_stores")
    connected: list[str] = []

    class Builder:
        async def build(self, settings: Any, context: Any) -> BuildResult[Any]:
            connected.append(context.resource_id)
            raise AssertionError("duplicate graph targets must fail before provider build")

    name = "test_same_target_primary"
    registry.register(
        name,
        ProviderDescriptor(
            ProviderRef("graph", name),
            Neo4jSettings,
            frozenset({"graph", "namespace_filter", "erase", "mirror"}),
            (),
            Builder(),
            target_identity=_neo4j_target_identity,
            requires_deployment_id=True,
        ),
    )
    monkeypatch.setenv("TEST_GRAPH_URL", "neo4j://example.test:7687")
    monkeypatch.setenv("TEST_GRAPH_USER", "test-user")
    monkeypatch.setenv("TEST_GRAPH_PASSWORD", "test-password")
    timeout_settings = {
        "url_env": "TEST_GRAPH_URL",
        "user_env": "TEST_GRAPH_USER",
        "password_env": "TEST_GRAPH_PASSWORD",
        "database": "same-db",
        "connect_timeout_s": 1.0,
        "request_timeout_s": 1.0,
    }
    engine = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "provider": "sqlite",
            "settings": {"path": str(tmp_path / "same-target.sqlite"), "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {}},
        graph={
            "provider": name,
            "settings": timeout_settings,
            "mirror": {"enabled": True, "provider": "neo4j", "settings": timeout_settings},
        },
        memories={"associative": {"enabled": True}},
        runtime={
            "deployment_id": "same-target-test",
            "provider_services": {
                "health_timeout_s": 1.0,
                "close_timeout_s": 1.0,
                "startup_timeout_s": None,
            },
        },
    )
    try:
        with pytest.raises(ConfigError, match="distinct canonical targets"):
            await engine.start()
        assert not connected
    finally:
        await engine.stop()
        registry.unregister(name)
