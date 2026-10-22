"""RM-72 / ADR-079: connected clients are shared, never duplicated inside or across Engines."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text

from memspine import Engine
from memspine.clients.weaviate import WeaviateClient, WeaviateSpec
from memspine.config.schema import EventLogConfig
from memspine.config.schema.providers import SqliteSettings
from memspine.config.schema.remote_providers import WeaviateSettings
from memspine.providers import shared
from memspine.providers.builtin import weaviate as weaviate_builtin
from memspine.providers.builtin.lexical import TantivyBuilder
from memspine.providers.builtin.storage import SqliteBuilder
from memspine.providers.contexts import (
    LexicalBuildContext,
    SharedBuildContext,
    StorageBuildContext,
    VectorBuildContext,
)
from memspine.providers.contracts import ProjectionPlacement
from memspine.providers.lifecycle import LifecycleState, ProviderLifecycleManager
from memspine.providers.shared import ResourceRegistry, shared_registry


@pytest.fixture(autouse=True)
def _fresh_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each test asserts on its own registry entries; other tests may deliberately leave
    a lease held (e.g. a startup test that makes a closer fail), so never share state."""
    monkeypatch.setattr(shared, "_REGISTRY", ResourceRegistry())


def _manager() -> ProviderLifecycleManager:
    return ProviderLifecycleManager(close_timeout_s=5.0)


def _storage_ctx(resources: ProviderLifecycleManager) -> StorageBuildContext:
    return StorageBuildContext(
        SharedBuildContext(resources, "owner", 2.0), EventLogConfig()
    )


async def _build_sqlite(path: str, resources: ProviderLifecycleManager) -> Any:
    settings = SqliteSettings(path=path, busy_timeout_s=5.0)
    return await SqliteBuilder().build(settings, _storage_ctx(resources))


def _categories() -> list[str]:
    return sorted(stats.key.category for stats in shared_registry().stats())


# ----------------------------------------------------------------------------- SQL
async def test_one_sync_engine_per_database_across_purposes(tmp_path: Path) -> None:
    resources = _manager()
    built = await _build_sqlite(str(tmp_path / "one.db"), resources)
    sql = built.sql
    outbox = sql.sync_lease(resources, "outbox")
    sleep = sql.sync_lease(resources, "sleep_leases")
    other = sql.sync_lease(resources, "pgvector")
    assert outbox.engine is sleep.engine is other.engine
    assert outbox.resource.resource_id == "sql:sync:outbox"
    with outbox.engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    report = await resources.close_all()
    assert report.complete
    # Labels close before the one shared engine, which closes before the async client.
    closed = list(report.closed)
    assert closed.index("sql:sync:outbox") < closed.index("sql:sync") < closed.index("sql")
    assert "sql.sqlite.sync" not in _categories()


async def test_memory_database_keeps_private_sync_engines_per_purpose() -> None:
    resources = _manager()
    built = await _build_sqlite(":memory:", resources)
    sql = built.sql
    first = sql.sync_lease(resources, "outbox")
    assert sql.sync_lease(resources, "outbox") is first
    assert sql.sync_lease(resources, "other").engine is not first.engine
    assert not _categories()  # nothing about a :memory: database is registered for sharing
    assert (await resources.close_all()).complete


async def test_identical_file_settings_share_client_and_stopping_one_keeps_other(
    tmp_path: Path,
) -> None:
    path = str(tmp_path / "shared.db")
    first_resources, second_resources = _manager(), _manager()
    first = await _build_sqlite(path, first_resources)
    second = await _build_sqlite(str(tmp_path / "." / "shared.db"), second_resources)
    assert first.sql.engine is second.sql.engine
    assert (
        first.sql.sync_lease(first_resources, "outbox").engine
        is second.sql.sync_lease(second_resources, "outbox").engine
    )

    assert (await first_resources.close_all()).complete
    async with second.sql.engine.connect() as connection:
        assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    with second.sql.sync_lease(second_resources, "outbox").engine.connect() as connection:
        assert connection.execute(text("SELECT 1")).scalar_one() == 1
    assert (await second_resources.close_all()).complete
    assert not _categories()


async def test_different_paths_and_memory_databases_are_isolated(tmp_path: Path) -> None:
    managers = [_manager() for _ in range(4)]
    a = await _build_sqlite(str(tmp_path / "a.db"), managers[0])
    b = await _build_sqlite(str(tmp_path / "b.db"), managers[1])
    m1 = await _build_sqlite(":memory:", managers[2])
    m2 = await _build_sqlite(":memory:", managers[3])
    assert a.sql.engine is not b.sql.engine
    assert m1.sql.engine is not m2.sql.engine
    async with m1.sql.engine.begin() as connection:
        await connection.execute(text("CREATE TABLE only_m1 (x INTEGER)"))
    async with m2.sql.engine.connect() as connection:
        rows = await connection.execute(
            text("SELECT name FROM sqlite_master WHERE name = 'only_m1'")
        )
        assert rows.first() is None
    for manager in managers:
        assert (await manager.close_all()).complete


async def test_two_engines_on_one_file_share_sql_and_survive_one_stop(tmp_path: Path) -> None:
    def make() -> Engine:
        return Engine(
            template="core",
            dotenv_path=None,
            storage={
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": str(tmp_path / "engines.db"), "busy_timeout_s": 5.0},
                },
                "cache": {"provider": "memory", "settings": {}},
            },
            embedding={"provider": "hash", "settings": {}},
            memories={"semantic": {"enabled": True}},
        )

    first, second = make(), make()
    await first.start()
    await second.start()
    try:
        assert first._sql is not None and second._sql is not None
        assert first._sql.engine is second._sql.engine
        await first.write("Ana likes green tea", namespace="a")
        await first.stop()
        assert first._provider_resources is not None
        assert first._provider_resources.states["sql"] is LifecycleState.STOPPED
        record = await second.write("Ben likes coffee", namespace="b")
        assert record is not None
        async with second._sql.engine.connect() as connection:
            assert (await connection.execute(text("SELECT 1"))).scalar_one() == 1
    finally:
        await second.stop()
        if first.is_started:
            await first.stop()


# ----------------------------------------------------------------------------- Weaviate
class _FakeWeaviateClient(WeaviateClient):
    """The real connector's identity and scope; no transport."""

    instances: list[_FakeWeaviateClient] = []

    def __init__(self, spec: WeaviateSpec) -> None:
        super().__init__(spec)
        self.connected = False
        self.closed = 0
        type(self).instances.append(self)

    @property
    def handle(self) -> Any:
        return SimpleNamespace()

    async def open(self) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed += 1

    async def health(self, deadline_s: float) -> bool:
        return self.connected


async def test_weaviate_vector_and_lexical_share_one_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memspine.clients.weaviate as weaviate_clients

    _FakeWeaviateClient.instances = []
    ensured: list[str] = []

    async def fake_ensure(native: object, name: str, *, include_content: bool) -> object:
        ensured.append(name)
        return SimpleNamespace(name=name)

    class _Store:
        def __init__(self, collection: object, **_: object) -> None:
            self.collection = collection

    monkeypatch.setattr(weaviate_clients, "WeaviateClient", _FakeWeaviateClient)
    monkeypatch.setattr(weaviate_builtin, "ensure_collection", fake_ensure)
    monkeypatch.setitem(
        __import__("sys").modules,
        "memspine.services.vector.weaviate_store",
        SimpleNamespace(WeaviateVectorStore=_Store),
    )
    monkeypatch.setitem(
        __import__("sys").modules,
        "memspine.services.lexical.weaviate_store",
        SimpleNamespace(WeaviateLexicalStore=_Store),
    )
    monkeypatch.setenv("MEMSPINE_WEAVIATE_URL", "http://weaviate.local:8080")
    settings = WeaviateSettings(connect_timeout_s=1.0, request_timeout_s=1.0)
    resources = _manager()
    shared = SharedBuildContext(resources, "deploy-a", 2.0)
    placement = ProjectionPlacement(None, True, "deploy-a")
    vector_ctx = VectorBuildContext(
        shared,
        placement,
        SimpleNamespace(embedder_id="hash", dim=8),  # type: ignore[arg-type]
        None,
        None,
        None,
        False,
        None,
    )
    vector = await weaviate_builtin.WeaviateVectorBuilder().build(settings, vector_ctx)
    lexical = await weaviate_builtin.WeaviateLexicalBuilder().build(
        settings.model_copy(update={"collection_prefix": "Other"}),
        LexicalBuildContext(shared, placement),
    )
    assert len(_FakeWeaviateClient.instances) == 1
    assert len(set(ensured)) == 2  # each service still ensures its own collection
    assert {ref.resource_id for ref in (*vector.resources, *lexical.resources)} == {
        "vector",
        "lexical",
    }
    report = await resources.close_all()
    assert report.complete
    assert _FakeWeaviateClient.instances[0].closed == 1


async def test_weaviate_different_credentials_do_not_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memspine.clients.weaviate as weaviate_clients

    _FakeWeaviateClient.instances = []
    monkeypatch.setattr(weaviate_clients, "WeaviateClient", _FakeWeaviateClient)
    monkeypatch.setenv("MEMSPINE_WEAVIATE_URL", "http://weaviate.local:8080")
    monkeypatch.setenv("KEY_A", "a")
    monkeypatch.setenv("KEY_B", "b")
    resources = _manager()
    base = WeaviateSettings(connect_timeout_s=1.0, request_timeout_s=1.0)
    await weaviate_builtin._shared_client(
        base.model_copy(update={"api_key_env": "KEY_A"}),
        SharedBuildContext(resources, "owner", 2.0),
        "a",
    )
    await weaviate_builtin._shared_client(
        base.model_copy(update={"api_key_env": "KEY_B"}),
        SharedBuildContext(resources, "owner", 2.0),
        "b",
    )
    assert len(_FakeWeaviateClient.instances) == 2
    for stats in shared_registry().stats():
        assert stats.key.identity != "a"  # keys are digests, never raw settings
    assert (await resources.close_all()).complete


# ----------------------------------------------------------------------------- Tantivy
def _lexical_ctx(resources: ProviderLifecycleManager, base: Path) -> LexicalBuildContext:
    return LexicalBuildContext(
        SharedBuildContext(resources, "owner", 2.0), ProjectionPlacement(base, False, "owner")
    )


async def test_tantivy_one_writer_per_path_and_memory_is_private(tmp_path: Path) -> None:
    from memspine.config.schema.providers import TantivySettings

    settings = TantivySettings()
    first_resources, second_resources = _manager(), _manager()
    first = await TantivyBuilder().build(settings, _lexical_ctx(first_resources, tmp_path / "db"))
    second = await TantivyBuilder().build(settings, _lexical_ctx(second_resources, tmp_path / "db"))
    assert first.service is second.service
    other_resources = _manager()
    other = await TantivyBuilder().build(settings, _lexical_ctx(other_resources, tmp_path / "o"))
    assert other.service is not first.service
    assert (await other_resources.close_all()).complete
    memory_ctx = LexicalBuildContext(
        SharedBuildContext(_manager(), "owner", 2.0), ProjectionPlacement(None, True, "owner")
    )
    memory_a = await TantivyBuilder().build(settings, memory_ctx)
    memory_b = await TantivyBuilder().build(
        settings,
        LexicalBuildContext(
            SharedBuildContext(_manager(), "owner", 2.0), ProjectionPlacement(None, True, "owner")
        ),
    )
    assert memory_a.service is not memory_b.service
    assert (await first_resources.close_all()).complete
    await second.service.search("default", "anything")  # still open for the other holder
    assert (await second_resources.close_all()).complete


def test_tantivy_shared_across_two_event_loops(tmp_path: Path) -> None:
    """Two Engines on different loops (the sync API runs one per Engine) share one writer."""
    from memspine.config.schema.providers import TantivySettings
    from memspine.store.records import MemoryRecord

    settings = TantivySettings()
    services: list[Any] = []
    managers: list[ProviderLifecycleManager] = []
    errors: list[BaseException] = []
    built = threading.Barrier(2)
    written = threading.Barrier(2)

    def run(index: int) -> None:
        async def main() -> None:
            resources = _manager()
            managers.append(resources)
            result = await TantivyBuilder().build(settings, _lexical_ctx(resources, tmp_path / "x"))
            services.append(result.service)
            await asyncio.to_thread(built.wait)
            for n in range(5):
                await result.service.index(
                    MemoryRecord(
                        content=f"word{index} item {n}", namespace="ns", memory_type="semantic"
                    )
                )
            await asyncio.to_thread(written.wait)
            hits = await result.service.search("ns", f"word{1 - index}")
            assert len(hits) == 5
            await resources.close_all()

        try:
            asyncio.run(main())
        except BaseException as exc:  # surfaced below
            errors.append(exc)
            built.abort()
            written.abort()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert not errors, errors
    assert services[0] is services[1]
    assert not [s for s in shared_registry().stats() if s.key.category == "lexical.tantivy"]


# ----------------------------------------------------------------------------- Ladybug
def test_ladybug_shared_across_two_event_loops(tmp_path: Path) -> None:
    pytest.importorskip("ladybug", reason="LadybugDB [graph] extra is not installed")
    from memspine.config.schema.providers import LadybugSettings
    from memspine.providers.builtin.graph import LadybugBuilder
    from memspine.providers.contexts import GraphBuildContext

    stores: list[Any] = []
    errors: list[BaseException] = []
    built = threading.Barrier(2)

    def run(index: int) -> None:
        async def main() -> None:
            resources = _manager()
            ctx = GraphBuildContext(
                SharedBuildContext(resources, "owner", 2.0),
                ProjectionPlacement(tmp_path / "g", False, "owner"),
                None,
            )
            result = await LadybugBuilder().build(LadybugSettings(), ctx)
            stores.append(result.service)
            await asyncio.to_thread(built.wait)
            await result.service.upsert_edge(f"a{index}", f"b{index}", "rel", namespace="ns")
            await resources.close_all()

        try:
            asyncio.run(main())
        except BaseException as exc:
            errors.append(exc)
            built.abort()

    threads = [threading.Thread(target=run, args=(i,)) for i in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)
    assert not errors, errors
    assert stores[0] is stores[1]


# ----------------------------------------------------------------------------- outbox
def _outbox_deps() -> tuple[Any, ProviderLifecycleManager]:
    from sqlalchemy.ext.asyncio import create_async_engine

    from memspine.clients.sqlite.sync import memory_sync_engine
    from memspine.providers.sql import ManagedSqlAccess

    async_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    resources = _manager()

    async def close_storage() -> None:
        await async_engine.dispose()

    async def checkpoint() -> None:
        return None

    resources.own("storage", close_storage)
    sql = ManagedSqlAccess(
        async_engine,
        is_memory=True,
        capabilities=frozenset({"sql"}),
        checkpoint=checkpoint,
        sync_factory=memory_sync_engine,
        owner_resource_id="storage",
    )
    return sql, resources


def _outbox_config(**workers: Any) -> Any:
    from memspine.config.schema import MemspineConfig

    return MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
        workers={"outbox": True, **workers},
    )


def test_runners_of_one_engine_share_one_job_store(monkeypatch: pytest.MonkeyPatch) -> None:
    from memspine.runtime import outbox as outbox_module
    from memspine.runtime.container import make_outbox_runner

    schema_checks = 0
    original = outbox_module.SqlJobStore.ensure_schema

    def counting(self: Any) -> None:
        nonlocal schema_checks
        schema_checks += 1
        original(self)

    monkeypatch.setattr(outbox_module.SqlJobStore, "ensure_schema", counting)
    sql, resources = _outbox_deps()
    other_sql, other_resources = _outbox_deps()
    try:
        config = _outbox_config()
        windows = make_outbox_runner(config, sql, resources)
        mirror = make_outbox_runner(config, sql, resources, label="graph_mirror")
        assert windows.store is mirror.store
        assert schema_checks == 1  # one create_all per Engine outbox database
        assert make_outbox_runner(config, other_sql, other_resources).store is not windows.store
    finally:
        assert asyncio.run(resources.close_all()).complete
        assert asyncio.run(other_resources.close_all()).complete


def test_taskiq_runners_of_one_engine_share_one_broker() -> None:
    pytest.importorskip("taskiq", reason="taskiq extra is not installed")
    from memspine.runtime.container import make_outbox_runner
    from memspine.runtime.transport import TASK_NAME, TransportRunner

    sql, resources = _outbox_deps()
    try:
        config = _outbox_config(transport="taskiq")
        windows = make_outbox_runner(config, sql, resources)
        mirror = make_outbox_runner(config, sql, resources, label="graph_mirror")
        assert isinstance(windows, TransportRunner) and isinstance(mirror, TransportRunner)
        assert windows.store is mirror.store
        assert windows.broker is mirror.broker
        assert windows.task_name == TASK_NAME
        assert mirror.task_name == f"{TASK_NAME}.graph_mirror"
    finally:
        assert asyncio.run(resources.close_all()).complete
