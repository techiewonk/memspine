"""MongoDB records store specifics beyond the shared conformance suite (RM-112).

Live: needs ``MEMSPINE_TEST_MONGO_URL`` (a replica set) and pymongo; each test uses a
throwaway database. The shared contract runs in ``test_records_conformance.py``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from memspine.exceptions import StorageError
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord

if TYPE_CHECKING:
    from memspine.clients.mongodb import MongoConnector
    from memspine.services.storage.mongodb import MongoRecordsStore

MONGO_URL = os.environ.get("MEMSPINE_TEST_MONGO_URL")
OWNED = os.environ.get("MEMSPINE_TEST_MONGO_OWNED") == "1"
pytestmark = pytest.mark.skipif(
    not MONGO_URL or importlib.util.find_spec("pymongo") is None,
    reason="MongoDB records store: set MEMSPINE_TEST_MONGO_URL (replica set)",
)


def _verify_owned(expected: tuple[str, str, int] | None = None) -> tuple[str, str, int]:
    path = Path(__file__).resolve().parents[4] / "scripts" / "local_services.py"
    spec = importlib.util.spec_from_file_location("local_services", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    state = launcher.load_state()
    if state is None:
        pytest.skip("owned RM-136 MongoDB stack is absent")
    port = launcher.ports_for(state)["MONGO"]
    project, owner = str(state["project"]), str(state["owner"])
    if expected is not None and expected != (project, owner, port):
        raise RuntimeError("owned MongoDB target changed during scratch database lifetime")
    ids = launcher.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=mongo",
            "--format",
            "{{.ID}}",
        ]
    ).stdout.split()
    if len(ids) != 1:
        raise RuntimeError("owned MongoDB container is missing or ambiguous")
    inspected = (
        launcher.run(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "com.docker.compose.project"}}|'
                '{{index .Config.Labels "com.docker.compose.service"}}|'
                '{{index .Config.Labels "memspine.rm136.owner"}}|'
                "{{.State.Status}}|{{json .NetworkSettings.Ports}}",
                ids[0],
            ]
        )
        .stdout.strip()
        .split("|", 4)
    )
    if len(inspected) != 5:
        raise RuntimeError("owned MongoDB container inspection was invalid")
    bound = json.loads(inspected[4]).get("27017/tcp")
    if inspected[:4] != [project, "mongo", owner, "running"] or bound != [
        {"HostIp": "127.0.0.1", "HostPort": str(port)}
    ]:
        raise RuntimeError("owned MongoDB container does not match its loopback port")
    expected_url = f"mongodb://127.0.0.1:{port}/?directConnection=true&replicaSet=rs0"
    if expected_url != MONGO_URL:
        raise RuntimeError("MongoDB test URL does not match the owned loopback replica set")
    return project, owner, port


@pytest.fixture
async def database() -> AsyncIterator[str]:
    from pymongo import AsyncMongoClient

    name = f"rm_{uuid.uuid4().hex[:12]}"
    owned = _verify_owned() if OWNED else None
    try:
        yield name
    finally:
        if owned is not None:
            _verify_owned(owned)
        admin: AsyncMongoClient[dict[str, object]] = AsyncMongoClient(MONGO_URL)
        await admin.drop_database(name)
        await admin.close()


async def _store(database: str, **kw: Any) -> tuple[MongoConnector, MongoRecordsStore]:
    from memspine.clients.mongodb import MongoConnector, MongoSpec
    from memspine.services.storage.mongodb import MongoRecordsStore

    assert MONGO_URL is not None
    connector = MongoConnector(MongoSpec(MONGO_URL, database=database))
    await connector.open()
    store = MongoRecordsStore(connector, **kw)
    await store.start()
    return connector, store


def _ev(**payload: object) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.MARKER, namespace="ns/a", payload={"marker": "test", **payload}
    )


async def test_compressed_log_round_trips_and_redacts(database: str) -> None:
    connector, store = await _store(database, compress=True)
    try:
        record = MemoryRecord(
            record_id="r1", namespace="ns/a", memory_type="episodic", content="hush"
        )
        written = await store.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="ns/a",
                payload={"record": record.model_dump(mode="json")},
            )
        )
        (back,) = await store.read_events()
        assert back.payload == written.payload and back.fingerprint == written.fingerprint
        assert await store.redact_event_payloads("r1") == [written.seq]
        (erased,) = await store.read_events()
        assert erased.payload["redacted"] is True and "hush" not in repr(erased.payload)
    finally:
        await store.stop()
        await connector.close()


async def test_malformed_write_is_rejected_before_event_commit(database: str) -> None:
    connector, store = await _store(database)
    try:
        with pytest.raises(StorageError, match="WRITE requires a record ID"):
            await store.append_event(
                MemoryEvent(kind=EventKind.WRITE, namespace="ns/a", payload={})
            )
        with pytest.raises(StorageError, match="WRITE requires a record ID"):
            await store.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE, namespace="ns/a", payload={"record": {"record_id": "r1"}}
                )
            )
        assert await store.read_events() == []
    finally:
        await store.stop()
        await connector.close()


async def test_rolling_prune_never_passes_the_slowest_projector(database: str) -> None:
    connector, store = await _store(database, mode=EventLogMode.ROLLING)
    try:
        seqs = [(await store.append_event(_ev(n=n))).seq for n in range(5)]
        assert all(seq is not None for seq in seqs)
        assert seqs[3] is not None and seqs[1] is not None
        future = datetime.now(UTC) + timedelta(days=1)
        assert await store.prune_events(future) == 0  # no projector has checkpointed yet
        await store.set_offset("vectors", seqs[3])
        await store.set_offset("graph", seqs[1])
        assert await store.prune_events(future) == 2
        assert [e.seq for e in await store.read_events()] == seqs[2:]
        nxt = await store.append_event(_ev(n=5))
        assert (
            nxt.seq is not None and seqs[-1] is not None and nxt.seq == seqs[-1] + 1
        )  # pruning never reuses a seq
    finally:
        await store.stop()
        await connector.close()


async def test_a_newer_schema_is_refused_and_ephemeral_is_rejected(database: str) -> None:
    from memspine.services.storage.mongodb import MongoRecordsStore

    connector, store = await _store(database)
    try:
        await connector.database["meta"].update_one({"_id": "schema"}, {"$set": {"version": 99}})
        with pytest.raises(StorageError, match="newer"):
            await store.start()
        with pytest.raises(StorageError, match="persists"):
            MongoRecordsStore(connector, mode=EventLogMode.EPHEMERAL)
    finally:
        await store.stop()
        await connector.close()


async def test_the_registered_factory_builds_a_started_store(database: str) -> None:
    """RM-112: ``storage.records: {provider: mongodb}`` builds through its descriptor (shared
    connector, no SQL access) and the store it returns is started by the Engine."""
    from memspine.config.schema.storage import EventLogConfig
    from memspine.providers import shared
    from memspine.providers.contexts import SharedBuildContext, StorageBuildContext
    from memspine.providers.contracts import StorageBuildResult
    from memspine.providers.lifecycle import ProviderLifecycleManager
    from memspine.providers.registry import get_registry, validate_settings

    assert MONGO_URL is not None
    descriptor = get_registry("storage_backends").get("mongodb")
    assert descriptor.locality == "cluster"
    settings = validate_settings(
        descriptor, {"url": MONGO_URL, "database": database, "collection_prefix": "m_"}
    )
    resources = ProviderLifecycleManager(close_timeout_s=5.0)
    ctx = StorageBuildContext(
        SharedBuildContext(resources, "owner", 5.0, registry=shared.ResourceRegistry()),
        EventLogConfig(),
    )
    result = await descriptor.builder.build(settings, ctx)
    assert isinstance(result, StorageBuildResult) and result.sql is None
    assert "sql" not in result.effective_capabilities
    store = result.service
    try:
        await store.start()
        connector = store._connector
        assert connector.topology is not None and connector.topology.startswith("replica_set:")
        assert await store.health()
        assert result.health_probe is not None and await result.health_probe()
        assert "m_events" in await connector.database.list_collection_names()
    finally:
        await store.stop()
        assert (await resources.close_all()).complete


async def test_ephemeral_mode_is_refused_by_the_factory() -> None:
    from memspine.config.schema.storage import EventLogConfig
    from memspine.exceptions import ConfigError
    from memspine.providers.contexts import SharedBuildContext, StorageBuildContext
    from memspine.providers.lifecycle import ProviderLifecycleManager
    from memspine.providers.registry import get_registry, validate_settings

    descriptor = get_registry("storage_backends").get("mongodb")
    settings = validate_settings(descriptor, {"url": "mongodb://127.0.0.1:1"})
    ctx = StorageBuildContext(
        SharedBuildContext(ProviderLifecycleManager(close_timeout_s=5.0), "owner", 5.0),
        EventLogConfig(mode=EventLogMode.EPHEMERAL),
    )
    with pytest.raises(ConfigError, match="ephemeral"):
        await descriptor.builder.build(settings, ctx)
