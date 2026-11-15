"""RM-34 ordered entity-index transactions on the owned RM-136 replica set."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine.clients.mongodb import MongoConnector, MongoSpec
from memspine.exceptions import RebuildUnavailableError, StorageError
from memspine.services.storage.entity_index_tables import EntityIndexRow
from memspine.services.storage.mongodb import MongoRecordsStore
from memspine.services.storage.records_port import EntityIndexEffects, EntityIndexProjectionView
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord

pytest.importorskip("pymongo")


def _owned_mongo(expected: tuple[str, str, int] | None = None) -> tuple[int, str, str]:
    path = Path(__file__).resolve().parents[2] / "scripts" / "local_services.py"
    spec = importlib.util.spec_from_file_location("local_services", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    state = launcher.load_state()
    if state is None:
        pytest.skip("owned RM-136 MongoDB stack is absent")
    port = launcher.ports_for(state)["MONGO"]
    project, owner = str(state["project"]), str(state["owner"])
    if expected is not None and (project, owner, port) != expected:
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
    return port, project, owner


class _Scratch:
    def __init__(self, port: int, database: str) -> None:
        self.port = port
        self.database = database

    def __repr__(self) -> str:
        return "<owned MongoDB scratch database>"

    @property
    def url(self) -> str:
        return f"mongodb://127.0.0.1:{self.port}/?directConnection=true&replicaSet=rs0"


@pytest.fixture
def scratch_mongo() -> Iterator[_Scratch]:
    from pymongo import MongoClient

    port, project, owner = _owned_mongo()
    scratch = _Scratch(port, f"rm34_eip_{uuid.uuid4().hex[:12]}")
    try:
        yield scratch
    finally:
        _owned_mongo((project, owner, port))
        admin: MongoClient[dict[str, Any]] = MongoClient(scratch.url, serverSelectionTimeoutMS=3000)
        with admin as client:
            client.drop_database(scratch.database)


async def _open(
    scratch: _Scratch, *, rolling: bool = False
) -> tuple[MongoRecordsStore, MongoConnector]:
    connector = MongoConnector(
        MongoSpec(scratch.url, database=scratch.database, connect_timeout_s=3)
    )
    await connector.open()
    store = MongoRecordsStore(
        connector, mode=EventLogMode.ROLLING if rolling else EventLogMode.FULL
    )
    try:
        await store.start()
    except BaseException:
        await connector.close()
        raise
    return store, connector


async def _append(store: MongoRecordsStore, namespace: str, name: str) -> int:
    event = await store.append_event(
        MemoryEvent(kind=EventKind.FORGET, namespace=namespace, payload={"record_id": name})
    )
    assert event.seq is not None
    return event.seq


async def _fold(
    view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
) -> EntityIndexEffects:
    await view.put(
        [
            EntityIndexRow(
                event.namespace,
                "link",
                str(event.payload["record_id"]),
                "entity",
                seq=int(event.seq or 0),
            )
            for event in events
        ]
    )
    return EntityIndexEffects(changed=bool(events))


async def _case_bounds_and_foreign(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        first = await _append(store, "a", "one")
        foreign = await _append(store, "b", "foreign")
        third = await _append(store, "a", "three")
        fourth = await _append(store, "a", "four")
        capped = await store.advance_entity_index("a", fourth, 1, _fold)
        assert (capped.namespace_cursor, capped.complete, capped.applied_count) == (first, False, 1)
        assert capped.global_offset == first
        target = await store.advance_entity_index("a", third, 10, _fold)
        assert (target.namespace_cursor, target.complete, target.applied_count) == (third, True, 1)
        assert target.global_offset == first  # unseen earlier foreign event blocks the prefix
        assert await store.entity_index_store().select("a", "link", key="four") == []
        peer = await store.advance_entity_index("b", fourth, 10, _fold)
        assert peer.namespace_cursor == fourth and peer.global_offset == third
        done = await store.advance_entity_index("a", fourth, 10, _fold)
        assert done.global_offset == fourth and done.namespace_cursor == fourth
        lower = await store.advance_entity_index("a", first, 10, _fold)
        assert (
            lower.namespace_cursor,
            lower.global_offset,
            lower.complete,
            lower.applied_count,
        ) == (
            fourth,
            fourth,
            True,
            0,
        )
        assert foreign < third
    finally:
        await store.stop()
        await connector.close()


async def _case_rollback_retry_purge_reset(scratch: _Scratch) -> None:
    store, connector = await _open(scratch, rolling=True)
    try:
        await store.initialize_entity_index()
        seq = await _append(store, "a", "one")
        await store.set_offset("vectors", seq)
        assert await store.prune_events(datetime.now(UTC) + timedelta(days=1)) == 0

        async def failed(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            await _fold(view, events)
            raise RuntimeError("forced callback rollback")

        with pytest.raises(RuntimeError, match="forced callback rollback"):
            await store.advance_entity_index("a", seq, 10, failed)
        assert await store.entity_index_store().select("a", "link") == []
        assert await store.entity_index_revision("a") == (0, 0)
        assert await store.get_offset("entity_index") == 0
        with pytest.raises(StorageError, match="ordered projection door"):
            await store.set_offset("entity_index", seq)

        attempts = 0

        async def conflicting(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            nonlocal attempts
            attempts += 1
            assert await view.select("a", "link", key="one") == [] or attempts > 1
            if attempts == 1:
                # A concurrent write after the transaction's snapshot forces a native retry.
                await store.entity_index_store().put(
                    [EntityIndexRow("a", "link", "one", "entity", seq=0)]
                )
            return await _fold(view, events)

        applied = await store.advance_entity_index("a", seq, 10, conflicting)
        assert attempts >= 2
        assert (applied.namespace_cursor, applied.namespace_mutation_revision) == (seq, 1)
        await store.initialize_entity_index()
        assert await store.get_offset("entity_index") == seq
        assert await store.entity_index_revision("a") == (0, 1)
        assert [row.seq for row in await store.entity_index_store().select("a", "link")] == [seq]

        async def purge(view: EntityIndexProjectionView, ids: Sequence[str]) -> EntityIndexEffects:
            assert tuple(ids) == ("one",)
            await view.delete("a", "link", key="one")
            return EntityIndexEffects(changed=True, removed_entity_ids=("entity",))

        removed = await store.purge_entity_index("a", ["one", "one"], purge)
        assert (removed.namespace_cursor, removed.namespace_mutation_revision) == (seq, 2)
        assert removed.effects.removed_entity_ids == ("entity",)
        assert await store.entity_index_store().select("a", "link") == []
        reset = await store.reset_entity_index()
        assert (reset.epoch, reset.namespace_cursor, reset.global_offset) == (1, 0, 0)
        assert await store.entity_index_revision("a") == (1, 0)
        assert await store.get_offset("entity_index") == 0
        assert await store.prune_events(datetime.now(UTC) + timedelta(days=1)) == 0
        await store.reset_offset("entity_index")
        assert await store.entity_index_revision("a") == (2, 0)
        assert await store.get_offset("entity_index") == 0
    finally:
        await store.stop()
        await connector.close()


async def _case_disabled_prune(scratch: _Scratch) -> None:
    store, connector = await _open(scratch, rolling=True)
    try:
        await store.assert_full_event_history()  # a genuinely empty log is complete
        seq = await _append(store, "a", "one")
        await store.set_offset("vectors", seq)
        assert await store.prune_events(datetime.now(UTC) + timedelta(days=1)) == 1
        assert await store.read_events() == []
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.assert_full_event_history()
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.initialize_entity_index()
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.advance_entity_index("a", seq, 10, _fold)
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.reset_entity_index()
        assert await store.entity_index_revision("a") == (0, 0)
        assert await store._c("offsets").find_one({"_id": "entity_index"}) is None
    finally:
        await store.stop()
        await connector.close()


async def _case_concurrent_namespaces(scratch: _Scratch) -> None:
    first, first_connector = await _open(scratch)
    second, second_connector = await _open(scratch)
    try:
        a = await _append(first, "a", "one")
        b = await _append(first, "b", "two")
        receipts = await asyncio.gather(
            first.advance_entity_index("a", b, 10, _fold),
            second.advance_entity_index("b", b, 10, _fold),
        )
        assert [r.namespace_cursor for r in receipts] == [b, b]
        assert await first.get_offset("entity_index") == b
        assert len(await first.entity_index_store().select("a", "link")) == 1
        assert len(await first.entity_index_store().select("b", "link")) == 1
        assert a < b

        entered, release = asyncio.Event(), asyncio.Event()

        async def held(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
            return await _fold(view, events)

        later = await _append(first, "a", "later")
        advancing = asyncio.create_task(first.advance_entity_index("a", later, 10, held))
        await asyncio.wait_for(entered.wait(), 8)
        resetting = asyncio.create_task(second.reset_entity_index())
        release.set()
        await asyncio.wait_for(advancing, 8)
        reset = await asyncio.wait_for(resetting, 8)
        assert reset.epoch == 1
        assert await first.entity_index_store().select("a", "link") == []
        assert await first.entity_index_revision("a") == (1, 0)
        assert await first.get_offset("entity_index") == 0
    finally:
        await first.stop()
        await second.stop()
        await first_connector.close()
        await second_connector.close()


async def _case_pending_and_namespace(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        await store.upsert_record(
            MemoryRecord(
                record_id="foreign", namespace="b", memory_type="episodic", content="secret"
            )
        )
        marker = await store.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={"marker": "pending-test", "record_id": "positive"},
            )
        )
        assert marker.seq is not None
        seq = marker.seq
        calls = 0

        async def pending(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            nonlocal calls
            calls += 1
            assert await view.get_record("foreign") is None
            with pytest.raises(StorageError, match="cross namespaces"):
                await view.put([EntityIndexRow("b", "link", "bad", "entity")])
            with pytest.raises(StorageError, match="cross namespaces"):
                await view.select("b", "link")
            with pytest.raises(StorageError, match="cross namespaces"):
                await view.delete("b", "link")
            if calls == 1:
                assert len(events) == 1
                assert await view.pending_events(1) == []
                await view.put(
                    [
                        EntityIndexRow(
                            "a", "pending_positive", str(seq), "merge-id", "merge", seq=seq
                        )
                    ]
                )
            else:
                assert events == []
                (canonical,) = await view.pending_events(1)
                assert canonical.seq == seq and canonical.payload == {
                    "marker": "pending-test",
                    "record_id": "positive",
                }
                await view.delete("a", "pending_positive", key=str(seq))
            return EntityIndexEffects(changed=True)

        first = await store.advance_entity_index("a", seq, 1, pending)
        assert (
            first.namespace_cursor,
            first.complete,
            first.applied_count,
            first.global_offset,
        ) == (seq - 1, False, 1, 0)
        second = await store.advance_entity_index("a", seq, 1, pending)
        assert (
            second.namespace_cursor,
            second.complete,
            second.applied_count,
            second.global_offset,
        ) == (seq, True, 0, seq)
        assert calls == 2
        assert await store.entity_index_revision("a") == (0, 2)
        assert await store.entity_index_store().select("b", "link") == []

        marker2 = await store.append_event(
            MemoryEvent(kind=EventKind.MARKER, namespace="a", payload={"marker": "pending-test-2"})
        )
        assert marker2.seq is not None
        seq2 = marker2.seq

        async def add_pending(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            assert [event.seq for event in events] == [seq2]
            await view.put(
                [
                    EntityIndexRow(
                        "a",
                        "pending_positive",
                        str(seq2),
                        "merge-id",
                        "merge",
                        seq=seq2,
                    )
                ]
            )
            return EntityIndexEffects(changed=True)

        blocked = await store.advance_entity_index("a", seq2, 1, add_pending)
        assert (blocked.namespace_cursor, blocked.complete) == (seq, False)
        foreign = await _append(store, "b", "foreign-after-pending")

        async def purge_pending(
            view: EntityIndexProjectionView, ids: Sequence[str]
        ) -> EntityIndexEffects:
            assert ids == ("source",)
            await view.delete("a", "pending_positive", key=str(seq2))
            return EntityIndexEffects(changed=True)

        purged = await store.purge_entity_index("a", ["source"], purge_pending)
        assert (purged.namespace_cursor, purged.complete, purged.global_offset) == (
            seq2,
            True,
            seq2,
        )
        assert await store.get_offset("entity_index") == seq2  # purge alone unlocked the prefix
        peer = await store.advance_entity_index("b", foreign, 1, _fold)
        assert peer.global_offset == foreign
    finally:
        await store.stop()
        await connector.close()


async def _case_registration_wins_prune(scratch: _Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    store, connector = await _open(scratch, rolling=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = MongoRecordsStore._entity_index_register

    async def held_register(self: MongoRecordsStore, session: Any, reset: Any) -> None:
        if self is store and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
        await original(self, session, reset)

    monkeypatch.setattr(MongoRecordsStore, "_entity_index_register", held_register)
    try:
        seq = await _append(store, "a", "one")
        await store.set_offset("vectors", seq)
        registering = asyncio.create_task(store.initialize_entity_index())
        await asyncio.wait_for(entered.wait(), 8)
        pruning = asyncio.create_task(store.prune_events(datetime.now(UTC) + timedelta(days=1)))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(registering, 8)
        assert await asyncio.wait_for(pruning, 8) == 0
        assert [event.seq for event in await store.read_events()] == [seq]
        await store.assert_full_event_history()
    finally:
        release.set()
        await store.stop()
        await connector.close()


async def _case_prune_wins_registration(scratch: _Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    from pymongo.asynchronous.collection import AsyncCollection

    store, connector = await _open(scratch, rolling=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = AsyncCollection.delete_many

    async def held_delete(self: Any, *args: Any, **kwargs: Any) -> Any:
        if self.name == "events" and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(AsyncCollection, "delete_many", held_delete)
    try:
        seq = await _append(store, "a", "one")
        await store.set_offset("vectors", seq)
        pruning = asyncio.create_task(store.prune_events(datetime.now(UTC) + timedelta(days=1)))
        await asyncio.wait_for(entered.wait(), 8)
        registering = asyncio.create_task(store.initialize_entity_index())
        await asyncio.sleep(0.05)
        release.set()
        assert await asyncio.wait_for(pruning, 8) == 1
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await asyncio.wait_for(registering, 8)
        assert await store._c("offsets").find_one({"_id": "entity_index"}) is None
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.assert_full_event_history()
    finally:
        release.set()
        await store.stop()
        await connector.close()


def test_mongo_entity_projection_bounds_and_foreign(scratch_mongo: _Scratch) -> None:
    asyncio.run(_case_bounds_and_foreign(scratch_mongo), loop_factory=asyncio.SelectorEventLoop)


def test_mongo_entity_projection_rollback_retry_purge_reset(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_rollback_retry_purge_reset(scratch_mongo), loop_factory=asyncio.SelectorEventLoop
    )


def test_mongo_entity_projection_concurrent_namespaces(scratch_mongo: _Scratch) -> None:
    asyncio.run(_case_concurrent_namespaces(scratch_mongo), loop_factory=asyncio.SelectorEventLoop)


def test_mongo_entity_projection_disabled_does_not_pin_pruning(scratch_mongo: _Scratch) -> None:
    asyncio.run(_case_disabled_prune(scratch_mongo), loop_factory=asyncio.SelectorEventLoop)


def test_mongo_entity_projection_pending_retry_and_namespace(scratch_mongo: _Scratch) -> None:
    asyncio.run(_case_pending_and_namespace(scratch_mongo), loop_factory=asyncio.SelectorEventLoop)


def test_mongo_entity_projection_registration_wins_prune(
    scratch_mongo: _Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _case_registration_wins_prune(scratch_mongo, monkeypatch),
        loop_factory=asyncio.SelectorEventLoop,
    )


def test_mongo_entity_projection_prune_wins_registration(
    scratch_mongo: _Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _case_prune_wins_registration(scratch_mongo, monkeypatch),
        loop_factory=asyncio.SelectorEventLoop,
    )
