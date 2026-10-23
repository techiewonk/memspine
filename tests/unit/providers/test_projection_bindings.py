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
            "records": {
                "provider": "sqlite",
                "settings": {"path": str(database), "busy_timeout_s": 5.0},
            },
            "graph": {"provider": graph_provider},
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
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


async def test_start_drops_leftover_graph_mirror_state(tmp_path: Path) -> None:
    """ADR-081 amendment 2026-10-23: a store that once ran the removed Neo4j graph mirror still
    holds its projector offset, binding and outbox rows. Start-up never fails on them; it
    deletes exactly those rows (and nothing else) and is idempotent."""
    from sqlalchemy import insert

    from memspine.providers.projection_bindings import (
        RETIRED_GRAPH_MIRROR_PROJECTOR,
        RETIRED_GRAPH_MIRROR_STAGE,
        retire_graph_mirror_state,
    )
    from memspine.services.storage.outbox_tables import outbox, outbox_item, outbox_progress
    from memspine.services.storage.sqlite.schema import (
        projector_offsets,
        provider_projector_bindings,
    )

    database = tmp_path / "old-mirror.sqlite"

    def _engine() -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            storage={
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": str(database), "busy_timeout_s": 5.0},
                },
                "graph": {"provider": "sql_adjacency"},
            },
            model={"embedding": {"provider": "hash", "settings": {}}},
            memories={"associative": {"enabled": True}},
            deployment={"id": "old-mirror-test"},
        )

    first = _engine()
    await first.start()
    await first.write("a record from before the mirror was removed", namespace="mirror-test")
    await first.stop()

    now = "2026-10-01T00:00:00+00:00"
    sql = create_async_engine(f"sqlite+aiosqlite:///{database}")

    def _scope(owner: str) -> dict[str, Any]:
        return {"tenant": "default", "owner": owner, "owner_kind": "user"}

    async with sql.begin() as conn:
        await conn.execute(
            insert(projector_offsets).values(
                projector_name=RETIRED_GRAPH_MIRROR_PROJECTOR, last_seq=1, updated_at=now
            )
        )
        await conn.execute(
            insert(provider_projector_bindings).values(
                deployment_id="old-mirror-test",
                projector_name=RETIRED_GRAPH_MIRROR_PROJECTOR,
                provider="graph:neo4j:v1",
                target_fingerprint="f" * 64,
                generation_id="active",
                updated_at=now,
            )
        )
        for owner, offset in (("graph-neo4j", 0), ("graph-neo4j", 1), ("someone-else", 0)):
            await conn.execute(
                insert(outbox_item).values(
                    **_scope(owner),
                    stage=RETIRED_GRAPH_MIRROR_STAGE,
                    item_offset=offset,
                    payload='{"op": "clear"}',
                    created_at=now,
                )
            )
        await conn.execute(
            insert(outbox_progress).values(
                **_scope("graph-neo4j"),
                job_kind=RETIRED_GRAPH_MIRROR_STAGE,
                watermark=-1,
                target_offset=1,
                holes="[]",
            )
        )
        await conn.execute(
            insert(outbox).values(
                job_id="old-mirror-job",
                seq=10_000,
                **_scope("graph-neo4j"),
                stage=RETIRED_GRAPH_MIRROR_STAGE,
                target_offset=1,
                state="queued",
                created_at=now,
                frozen=False,
                epoch=0,
                attempts=0,
            )
        )

    async def _count(table: Any, *where: Any) -> int:
        async with sql.connect() as conn:
            return int(
                await conn.scalar(select(func.count()).select_from(table).where(*where)) or 0
            )

    async def _left() -> dict[str, int]:
        return {
            "offset": await _count(
                projector_offsets,
                projector_offsets.c.projector_name == RETIRED_GRAPH_MIRROR_PROJECTOR,
            ),
            "binding": await _count(
                provider_projector_bindings,
                provider_projector_bindings.c.projector_name == RETIRED_GRAPH_MIRROR_PROJECTOR,
            ),
            "items": await _count(outbox_item, outbox_item.c.stage == RETIRED_GRAPH_MIRROR_STAGE),
            "progress": await _count(
                outbox_progress, outbox_progress.c.job_kind == RETIRED_GRAPH_MIRROR_STAGE
            ),
            "jobs": await _count(outbox, outbox.c.stage == RETIRED_GRAPH_MIRROR_STAGE),
        }

    assert await _left() == {"offset": 1, "binding": 1, "items": 3, "progress": 1, "jobs": 1}
    second = _engine()
    await second.start()  # never fails on the old rows
    try:
        assert await second.search("before the mirror", namespace="mirror-test", top_k=1)
    finally:
        await second.stop()
    # only the mirror's own scope went; another scope's item under the same stage name stays
    assert await _left() == {"offset": 0, "binding": 0, "items": 1, "progress": 0, "jobs": 0}

    third = _engine()  # idempotent: a clean store starts and nothing more is removed
    await third.start()
    await third.stop()
    assert await _left() == {"offset": 0, "binding": 0, "items": 1, "progress": 0, "jobs": 0}

    class _Storage:
        async def get_offset(self, name: str) -> int:
            return 0

        async def reset_offset(self, name: str) -> None:
            raise AssertionError("nothing to reset")

    removed = await retire_graph_mirror_state(_Storage(), sql)
    assert not any(removed.values())
    await sql.dispose()
