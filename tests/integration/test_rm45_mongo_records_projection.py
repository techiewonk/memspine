"""RM-45 ordered records projection on the owned RM-136 MongoDB replica set."""

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
from memspine.schemas.durable.assessment import Assessment
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.evidence import Evidence, TurnRef
from memspine.schemas.durable.identity import AssertionKey
from memspine.schemas.durable.semantic import Mention, PredicateTyping, SemanticEnvelope
from memspine.semantics.registry import load_builtin_registry
from memspine.services.storage.mongodb import MongoRecordsStore
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.records_port import OutboxItem, RecordsProjectionView
from memspine.store.claim_cardinality import group_key, record_group_of
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord, RecordFingerprint
from memspine.store.registry_evidence import scrub_registry_marker

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
    scratch = _Scratch(port, f"rm45_rec_{uuid.uuid4().hex[:12]}")
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


def _record(record_id: str, *, namespace: str = "a") -> MemoryRecord:
    return MemoryRecord(
        record_id=record_id, namespace=namespace, memory_type="episodic", content="private text"
    )


async def _write(store: MongoRecordsStore, record: MemoryRecord) -> int:
    event = await store.append_event(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=record.namespace,
            payload={"record": record.model_dump(mode="json")},
        )
    )
    assert event.seq is not None
    return event.seq


async def _fold(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
    for event in events:
        raw = event.payload.get("record")
        if isinstance(raw, dict):
            await view.put_record(MemoryRecord.model_validate(raw))


def _claim(record_id: str, value: str) -> MemoryRecord:
    registry = load_builtin_registry()
    env = SemanticEnvelope(
        write_id=f"write-{value}",
        assertion_key=AssertionKey(src="mira", rel="born_in", dst=value, kind="state"),
        occurrence_id=f"occ-{value}",
        scope=OwnerScope(tenant="tenant-a", owner="person-a"),
        text=f"Mira was born in {value}",
        kind="state",
        subject=Mention(surface="Mira", mention_id="mira-mention", entity_id="entity-a"),
        attribute="born_in",
        value=value,
        evidence=[
            Evidence(
                role="support",
                turn=TurnRef(source_id="source-a", source_version=1, seg_index=0),
                source_family="Mira",
            )
        ],
        assessment=Assessment(source_trust=0.8, binding="bound", modality="asserted"),
        src_class="user_stated",
        registry=PredicateTyping(
            predicate="born_in",
            predicate_version=registry.predicates["born_in"].version,
            registry_digest=registry.digest,
            subject_type="person",
            object_kind="entity",
            object_entity_type="place",
            cardinality="one",
            cardinality_scope={"precision": "city"},
        ),
    )
    return MemoryRecord(
        record_id=record_id,
        namespace="a",
        memory_type="semantic",
        content=env.text,
        entity="Mira",
        attribute="born_in",
        fingerprint=RecordFingerprint(envelope=env.model_dump(mode="json")),
    )


async def _case_atomic_order_and_group(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        records = [
            _claim("claim-c", "Lisbon"),
            _claim("claim-a", "Porto"),
            _claim("claim-b", "Paris"),
        ]
        seqs = [await _write(store, record) for record in records]
        await store.initialize_records_projection()
        with pytest.raises(StorageError, match="ordered projection door"):
            await store.set_offset("records", seqs[-1])
        first = await store.advance_records(seqs[-1], 1, _fold)
        assert (first.cursor, first.applied_count, first.complete) == (seqs[0], 1, False)
        assert await store.get_offset("records") == seqs[0]

        async def failed(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await _fold(view, events)
            await view.mark_erased("a", "source-a")
            raise RuntimeError("rollback")

        with pytest.raises(RuntimeError, match="rollback"):
            await store.advance_records(seqs[-1], 3, failed)
        assert await store.get_record("claim-a") is None
        assert await store.get_offset("records") == seqs[0]
        assert await store._c("records_projection_membership").count_documents({}) == 0

        key_group = record_group_of(records[0])
        assert key_group is not None
        key = group_key(key_group)

        async def finish(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await _fold(view, events)
            one = await view.group_page("a", key, after_id=None, limit=2)
            two = await view.group_page("a", key, after_id=one[-1].record_id, limit=2)
            assert [row.record_id for row in one + two] == ["claim-a", "claim-b", "claim-c"]
            assert await view.group_page("b", key, after_id=None, limit=2) == []

        done = await store.advance_records(seqs[-1], 3, finish)
        assert (done.cursor, done.applied_count, done.complete) == (seqs[-1], 2, True)
        assert await store.get_offset("records") == seqs[-1]
        lower = await store.advance_records(seqs[0], 3, _fold)
        assert (lower.cursor, lower.applied_count, lower.complete) == (seqs[-1], 0, True)
        indexed = await store._c("records").find_one({"_id": "claim-a"}, {"claim_group_key": 1})
        assert indexed is not None and indexed["claim_group_key"] == key
        public_page = await store.claim_group_page("a", key, after_id=None, limit=2)
        assert [row.record_id for row in public_page] == ["claim-a", "claim-b"]
        assert [
            row.record_id
            for row in await store.claim_group_page(
                "a", key, after_id=public_page[-1].record_id, limit=2
            )
        ] == ["claim-c"]
        assert [row.record_id for row in await store.claim_group_snapshot("a", key)] == [
            "claim-a",
            "claim-b",
            "claim-c",
        ]
    finally:
        await store.stop()
        await connector.close()


async def _case_erasure_reset_prune(scratch: _Scratch) -> None:
    store, connector = await _open(scratch, rolling=True)
    try:
        await store.initialize_records_projection()
        seq = await _write(store, _record("r1"))
        await store.set_offset("vectors", seq)

        async def erasure(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await _fold(view, events)
            await view.mark_erased("a", "r1")
            await view.mark_hidden("a", "r1")
            assert await view.erasure_state("a", "r1") == (True, True)
            await view.release_hidden("a", "r1")
            assert await view.erasure_state("a", "r1") == (True, False)

        await store.advance_records(seq, 10, erasure)
        docs = [doc async for doc in store._c("records_projection_membership").find({})]
        assert len(docs) == 1 and docs[0]["kind"] == "erased"
        assert "private text" not in repr(docs) and "Lisbon" not in repr(docs)
        await store.reset_records_projection()
        assert await store.get_record("r1") is None
        assert await store.get_offset("records") == 0
        assert await store._c("records_projection_membership").count_documents({}) == 0
        assert await store.prune_events(datetime.now(UTC) + timedelta(days=1)) == 0
        with pytest.raises(StorageError, match="ephemeral"):
            await store.append_ephemeral_records(
                MemoryEvent(kind=EventKind.WRITE, namespace="a"), _fold
            )
    finally:
        await store.stop()
        await connector.close()


async def _case_disabled_pruned_refusal(scratch: _Scratch) -> None:
    store, connector = await _open(scratch, rolling=True)
    try:
        seq = await _write(store, _record("r1"))
        await store.set_offset("vectors", seq)
        assert await store.prune_events(datetime.now(UTC) + timedelta(days=1)) == 1
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.initialize_records_projection()
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.advance_records(seq, 10, _fold)
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.reset_records_projection()
        assert await store._c("offsets").find_one({"_id": "records"}) is None
    finally:
        await store.stop()
        await connector.close()


async def _case_concurrent_clients(scratch: _Scratch) -> None:
    first, first_connector = await _open(scratch)
    second, second_connector = await _open(scratch)
    entered, release = asyncio.Event(), asyncio.Event()
    try:
        seq = await _write(first, _record("r1"))
        await first.initialize_records_projection()

        async def held(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
            await _fold(view, events)

        advancing = asyncio.create_task(first.advance_records(seq, 10, held))
        await asyncio.wait_for(entered.wait(), 8)
        competing = asyncio.create_task(second.advance_records(seq, 10, _fold))
        await asyncio.sleep(0.05)
        release.set()
        a, b = await asyncio.wait_for(asyncio.gather(advancing, competing), 8)
        assert sorted([a.applied_count, b.applied_count]) == [0, 1]
        assert a.cursor == b.cursor == seq
        assert await first._c("records").count_documents({}) == 1
    finally:
        release.set()
        await first.stop()
        await second.stop()
        await first_connector.close()
        await second_connector.close()


async def _case_runtime_conflict_and_erasure(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        await store.initialize_records_projection()
        projector = RecordProjector(store)
        first = _claim("claim-a", "Lisbon")
        rival = _claim("claim-b", "Porto")
        for record in (first, rival):
            event = await store.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="a",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
        await projector.apply(event)
        for record_id in ("claim-a", "claim-b"):
            projected = await store.get_record(record_id)
            assert projected is not None
            assert projected.fingerprint.envelope is not None
            assert projected.fingerprint.envelope["cardinality_conflict"] is True

        forgotten = await store.append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="a",
                payload={"record_id": "claim-b", "hard": True},
            )
        )
        await projector.apply(forgotten)
        survivor = await store.get_record("claim-a")
        assert survivor is not None and survivor.fingerprint.envelope is not None
        assert survivor.fingerprint.envelope["cardinality_conflict"] is False
        assert await store.get_record("claim-b") is None
        membership = [doc async for doc in store._c("records_projection_membership").find({})]
        assert len(membership) == 1 and membership[0]["record_id"] == "claim-b"
        assert "Lisbon" not in repr(membership) and "Porto" not in repr(membership)
    finally:
        await store.stop()
        await connector.close()


async def _case_reset_wins_prune(scratch: _Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    store, connector = await _open(scratch, rolling=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = MongoRecordsStore._records_register

    async def held_register(self: MongoRecordsStore, session: Any, reset: Any, cursor: int) -> None:
        if self is store and cursor > 0 and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
        await original(self, session, reset, cursor)

    try:
        await store.initialize_records_projection()
        seq = await _write(store, _record("r1"))
        await store.advance_records(seq, 10, _fold)
        await store.set_offset("vectors", seq)
        monkeypatch.setattr(MongoRecordsStore, "_records_register", held_register)
        resetting = asyncio.create_task(store.reset_records_projection())
        await asyncio.wait_for(entered.wait(), 8)
        pruning = asyncio.create_task(store.prune_events(datetime.now(UTC) + timedelta(days=1)))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(resetting, 8)
        assert await asyncio.wait_for(pruning, 8) == 0
        assert await store.get_record("r1") is None
        assert await store.get_offset("records") == 0
        assert [event.seq for event in await store.read_events()] == [seq]
    finally:
        release.set()
        await store.stop()
        await connector.close()


async def _case_prune_wins_reset(scratch: _Scratch, monkeypatch: pytest.MonkeyPatch) -> None:
    from pymongo.asynchronous.collection import AsyncCollection

    store, connector = await _open(scratch, rolling=True)
    entered, release = asyncio.Event(), asyncio.Event()
    original = AsyncCollection.delete_many

    async def held_delete(self: Any, *args: Any, **kwargs: Any) -> Any:
        if self.name == "events" and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
        return await original(self, *args, **kwargs)

    try:
        await store.initialize_records_projection()
        seq = await _write(store, _record("r1"))
        await store.advance_records(seq, 10, _fold)
        await store.set_offset("vectors", seq)
        monkeypatch.setattr(AsyncCollection, "delete_many", held_delete)
        pruning = asyncio.create_task(store.prune_events(datetime.now(UTC) + timedelta(days=1)))
        await asyncio.wait_for(entered.wait(), 8)
        resetting = asyncio.create_task(store.reset_records_projection())
        await asyncio.sleep(0.05)
        release.set()
        assert await asyncio.wait_for(pruning, 8) == 1
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await asyncio.wait_for(resetting, 8)
        assert await store.get_record("r1") is not None
        assert await store.get_offset("records") == seq
    finally:
        release.set()
        await store.stop()
        await connector.close()


async def _case_append_fence_and_reset_replay(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        await store.initialize_records_projection()
        record = _record("hard-id")
        write = await store.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                payload={"record": record.model_dump(mode="json")},
            )
        )
        await store.advance_records(int(write.seq or 0), 10, _fold)
        assert await store.get_record("hard-id") is not None
        hard = await store.append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="a",
                payload={"record_id": "hard-id", "hard": True},
            )
        )
        fence = await store._c("records_append_fence").find_one({"_id": "hard-id"})
        assert fence is not None and fence["namespace"] == "a"
        assert "private text" not in repr(fence)

        before = await store._c("events").count_documents({})
        fresh = MemoryEvent(
            kind=EventKind.WRITE,
            namespace="a",
            payload={"record": record.model_dump(mode="json")},
        )
        with pytest.raises(StorageError, match="permanently erased"):
            await store.append_event(fresh)
        with pytest.raises(StorageError, match="permanently erased"):
            await store.append_with_outbox(
                fresh,
                [OutboxItem(scope=OwnerScope(tenant="tenant-a", owner="person-a"), stage="derive")],
            )
        with pytest.raises(StorageError, match="another namespace"):
            await store.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="b",
                    payload={
                        "record": record.model_copy(update={"namespace": "b"}).model_dump(
                            mode="json"
                        )
                    },
                )
            )
        assert await store._c("events").count_documents({}) == before
        assert await store._c("outbox").count_documents({}) == 0

        # The fence denies the historical WRITE before the hard FORGET is folded.
        await store.reset_records_projection()
        assert await store._c("records_append_fence").count_documents({}) == 1
        projector = RecordProjector(store)
        await projector.apply(write)
        assert await store.get_record("hard-id") is None
        with pytest.raises(StorageError, match="permanently erased"):
            await store.upsert_record(record)
        assert await store.get_record("hard-id") is None
        await projector.apply(hard)
        assert await store.get_record("hard-id") is None

        soft = await store.append_event(
            MemoryEvent(kind=EventKind.FORGET, namespace="a", payload={"record_id": "soft-id"})
        )
        assert soft.seq is not None
        assert await store._c("records_append_fence").find_one({"_id": "soft-id"}) is None
        await _write(store, _record("soft-id"))

        manifest = await store.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={
                    "marker": "erasure_manifest",
                    "manifest": {"ids": {"record_ids": ["manifest-id"]}},
                },
            )
        )
        assert manifest.seq is not None
        assert await store._c("records_append_fence").find_one({"_id": "manifest-id"})
        with pytest.raises(StorageError, match="permanently erased"):
            await _write(store, _record("manifest-id"))
    finally:
        await store.stop()
        await connector.close()


async def _case_foreign_erasure_refusal(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        record = _record("victim", namespace="a")
        seq = await _write(store, record)
        # The immutable append identity must protect even an unprojected WRITE.
        assert await store.get_record("victim") is None
        before = await store._c("events").count_documents({})
        with pytest.raises(StorageError, match="another namespace"):
            await store.append_event(
                MemoryEvent(
                    kind=EventKind.FORGET,
                    namespace="b",
                    payload={"record_id": "victim", "hard": True},
                )
            )
        assert await store._c("events").count_documents({}) == before
        assert await store._c("records_append_fence").count_documents({}) == 0
        await store.initialize_records_projection()
        await store.advance_records(seq, 10, _fold)
        assert await store.get_record("victim") is not None
        await _write(store, record)
    finally:
        await store.stop()
        await connector.close()


async def _case_foreign_mutators_and_snapshot(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        await _write(store, _record("owned", namespace="a"))  # deliberately unprojected
        before = await store._c("events").count_documents({})
        foreign = [
            MemoryEvent(kind=EventKind.FORGET, namespace="b", payload={"record_id": "owned"}),
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace="b",
                payload={"record_id": "owned", "set": {"tier": "cold"}},
            ),
            MemoryEvent(
                kind=EventKind.FEEDBACK,
                namespace="b",
                payload={"record_id": "owned", "signal": "like"},
            ),
            MemoryEvent(kind=EventKind.RETRIEVE, namespace="b", payload={"record_ids": ["owned"]}),
            MemoryEvent(
                kind=EventKind.SESSION,
                namespace="b",
                payload={"record_ids": ["owned"], "state": "passive"},
            ),
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="b",
                payload={"marker": "erasure_release", "record_ids": ["owned"]},
            ),
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="b",
                payload={
                    "marker": "erasure_manifest",
                    "manifest": {"ids": {"record_ids": []}},
                    "hidden": ["owned"],
                },
            ),
        ]
        for event in foreign:
            with pytest.raises(StorageError, match="another namespace"):
                await store.append_event(event)
        assert await store._c("events").count_documents({}) == before
        with pytest.raises(StorageError, match="another namespace"):
            await store.upsert_record(_record("owned", namespace="b"))
        assert await store.get_record("owned") is None

        await store.initialize_records_projection()
        key_group = record_group_of(_claim("sample", "Lisbon"))
        assert key_group is not None
        key = group_key(key_group)
        marker = await store.append_event(
            MemoryEvent(kind=EventKind.MARKER, namespace="a", payload={"marker": "bulk-test"})
        )
        assert marker.seq is not None

        async def bulk(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await _fold(view, events)
            for i in range(260):
                await view.put_record(_claim(f"bulk-{i:03d}", "Lisbon"))

        await store.advance_records(marker.seq, 10, bulk)
        snapshot = await store.claim_group_snapshot("a", key)
        assert len(snapshot) == 260
        assert snapshot[0].record_id == "bulk-000"
        assert snapshot[-1].record_id == "bulk-259"
    finally:
        await store.stop()
        await connector.close()


async def _case_concurrent_fence_order(
    scratch: _Scratch, *, hard_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    first, first_connector = await _open(scratch)
    second, second_connector = await _open(scratch)
    entered, release = asyncio.Event(), asyncio.Event()
    original = MongoRecordsStore._next

    async def held_next(self: MongoRecordsStore, counter: Any, *args: Any, **kwargs: Any) -> int:
        seq = await original(self, counter, *args, **kwargs)
        if self is first and counter == "events" and not entered.is_set():
            entered.set()
            await asyncio.wait_for(release.wait(), 8)
        return seq

    monkeypatch.setattr(MongoRecordsStore, "_next", held_next)
    try:
        rid = "race-hard-first" if hard_first else "race-write-first"
        write = MemoryEvent(
            kind=EventKind.WRITE,
            namespace="a",
            payload={"record": _record(rid).model_dump(mode="json")},
        )
        hard = MemoryEvent(
            kind=EventKind.FORGET,
            namespace="a",
            payload={"record_id": rid, "hard": True},
        )
        leading = asyncio.create_task(first.append_event(hard if hard_first else write))
        await asyncio.wait_for(entered.wait(), 8)
        following = asyncio.create_task(second.append_event(write if hard_first else hard))
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(leading, 8)
        if hard_first:
            with pytest.raises(StorageError, match="permanently erased"):
                await asyncio.wait_for(following, 8)
            assert await first._c("events").count_documents({}) == 1
        else:
            await asyncio.wait_for(following, 8)
            assert await first._c("events").count_documents({}) == 2
        assert await first._c("records_append_fence").count_documents({"_id": rid}) == 1
    finally:
        release.set()
        await first.stop()
        await second.stop()
        await first_connector.close()
        await second_connector.close()


async def _case_terminal_payload_validation(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        bad_hard: list[dict[str, Any]] = [
            {"hard": True},
            {"hard": True, "record_id": None},
            {"hard": True, "record_id": ""},
            {"hard": True, "record_id": 7},
        ]
        bad_manifest: list[dict[str, Any]] = [
            {"marker": "erasure_manifest", "manifest": "bad"},
            {"marker": "erasure_manifest", "manifest": {"ids": "bad"}},
            {"marker": "erasure_manifest", "manifest": {"ids": {"record_ids": "bad"}}},
            {"marker": "erasure_manifest", "manifest": {"ids": {"record_ids": ["ok", ""]}}},
            {"marker": "erasure_manifest", "manifest": {"ids": {"record_ids": [42]}}},
        ]
        outbox = [OutboxItem(scope=OwnerScope(tenant="tenant-a", owner="person-a"), stage="derive")]
        for payload in bad_hard:
            event = MemoryEvent(kind=EventKind.FORGET, namespace="a", payload=payload)
            with pytest.raises(StorageError, match="nonempty record_id"):
                await store.append_with_outbox(event, outbox)
        for payload in bad_manifest:
            event = MemoryEvent(kind=EventKind.MARKER, namespace="a", payload=payload)
            with pytest.raises(StorageError, match="erasure manifest"):
                await store.append_with_outbox(event, outbox)
        bad_transitions = [
            MemoryEvent(kind=EventKind.FORGET, namespace="a", payload={}),
            MemoryEvent(kind=EventKind.FORGET, namespace="a", payload={"record_id": ""}),
            MemoryEvent(kind=EventKind.DECAY_TRANSITION, namespace="a", payload={}),
            MemoryEvent(kind=EventKind.RETRIEVE, namespace="a", payload={"record_ids": "bad"}),
        ]
        for event in bad_transitions:
            with pytest.raises(StorageError, match="record transition|record_ids"):
                await store.append_with_outbox(event, outbox)
        assert await store._c("events").count_documents({}) == 0
        assert await store._c("outbox").count_documents({}) == 0
        assert await store._c("records_append_fence").count_documents({}) == 0

        for payload in (
            {"marker": "erasure_manifest"},
            {"marker": "erasure_manifest", "manifest": {"ids": {"record_ids": []}}},
        ):
            await store.append_event(
                MemoryEvent(kind=EventKind.MARKER, namespace="a", payload=payload)
            )
        for _ in range(2):
            await store.append_event(
                MemoryEvent(
                    kind=EventKind.FORGET,
                    namespace="a",
                    payload={"hard": True, "record_id": "valid-id"},
                )
            )
            await store.append_event(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace="a",
                    payload={
                        "marker": "erasure_manifest",
                        "manifest": {"ids": {"record_ids": ["valid-id"]}},
                    },
                )
            )
        assert await store._c("events").count_documents({}) == 6
        assert await store._c("records_append_fence").count_documents({"_id": "valid-id"}) == 1
    finally:
        await store.stop()
        await connector.close()


async def _case_peer_visibility_snapshot(scratch: _Scratch) -> None:
    writer, writer_connector = await _open(scratch)
    peer, peer_connector = await _open(scratch)
    try:
        await writer.initialize_records_projection()
        first = _claim("claim-a", "Lisbon")
        rival = _claim("claim-b", "Porto")
        key_group = record_group_of(first)
        assert key_group is not None
        key = group_key(key_group)
        for record in (first, rival):
            event = await writer.append_event(
                MemoryEvent(
                    kind=EventKind.WRITE,
                    namespace="a",
                    payload={"record": record.model_dump(mode="json")},
                )
            )
        projector = RecordProjector(writer)
        await projector.apply(event)
        assert [r.record_id for r in await peer.claim_group_snapshot("a", key)] == [
            "claim-a",
            "claim-b",
        ]

        hidden = await writer.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={
                    "marker": "erasure_manifest",
                    "manifest": {"ids": {"record_ids": []}},
                    "hidden": ["claim-b"],
                },
            )
        )
        await projector.apply(hidden)
        assert [r.record_id for r in await peer.claim_group_snapshot("a", key)] == ["claim-a"]
        sample_ids = [f"absent-{i}" for i in range(600)] + ["claim-a", "claim-b"]
        assert await peer.hidden_or_erased_record_ids("a", sample_ids) == {"claim-b"}
        assert await peer.hidden_or_erased_record_ids("b", ["claim-b"]) == set()

        release = await writer.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={"marker": "erasure_release", "record_ids": ["claim-b"]},
            )
        )
        await projector.apply(release)
        assert [r.record_id for r in await peer.claim_group_snapshot("a", key)] == [
            "claim-a",
            "claim-b",
        ]
        hard = await writer.append_event(
            MemoryEvent(
                kind=EventKind.FORGET, namespace="a", payload={"record_id": "claim-b", "hard": True}
            )
        )
        await projector.apply(hard)
        assert [r.record_id for r in await peer.claim_group_snapshot("a", key)] == ["claim-a"]
        assert await peer.hidden_or_erased_record_ids("b", ["claim-b"]) == {"claim-b"}
    finally:
        await writer.stop()
        await peer.stop()
        await writer_connector.close()
        await peer_connector.close()


async def _case_untyped_incumbent_lookup(
    scratch: _Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, connector = await _open(scratch)
    try:
        base = datetime(2026, 1, 1, tzinfo=UTC)
        berlin = MemoryRecord(
            record_id="untyped-berlin",
            namespace="a",
            memory_type="semantic",
            content="Berlin",
            entity="Mira",
            attribute="born_in",
            recorded_at=base,
        )
        lisbon = _claim("typed-lisbon", "Lisbon").model_copy(
            update={"recorded_at": base + timedelta(days=1)}
        )
        porto = MemoryRecord(
            record_id="untyped-porto",
            namespace="a",
            memory_type="semantic",
            content="Porto",
            entity="Mira",
            attribute="born_in",
            recorded_at=base + timedelta(days=2),
        )
        assert record_group_of(lisbon) is not None
        for row in (berlin, lisbon, porto):
            await store.upsert_record(row)
        latest = await store.find_active_untyped_fact("a", "Mira", "born_in")
        assert latest is not None and latest.record_id == "untyped-porto"

        await store.upsert_record(porto.model_copy(update={"valid_to": base + timedelta(days=3)}))
        # Older rows lacking the physical index field remain eligible.
        await store._c("records").update_one(
            {"_id": berlin.record_id}, {"$unset": {"claim_group_key": ""}}
        )

        async def no_scan(*args: Any, **kwargs: Any) -> Any:
            raise AssertionError("untyped lookup must use the keyed query")

        monkeypatch.setattr(store, "list_records", no_scan)
        incumbent = await store.find_active_untyped_fact("a", "Mira", "born_in")
        ordinary = await store.find_active_fact("a", "Mira", "born_in")
        assert incumbent is not None and incumbent.record_id == "untyped-berlin"
        assert ordinary is not None and ordinary.record_id == "typed-lisbon"
        assert await store.find_active_untyped_fact("b", "Mira", "born_in") is None
        indexes = await store._c("records").index_information()
        assert "ns_untyped_fact" in indexes
    finally:
        await store.stop()
        await connector.close()


def test_mongo_records_projection_atomic_order_group(scratch_mongo: _Scratch) -> None:
    asyncio.run(_case_atomic_order_and_group(scratch_mongo), loop_factory=asyncio.SelectorEventLoop)


def test_mongo_records_projection_erasure_reset_prune(scratch_mongo: _Scratch) -> None:
    asyncio.run(_case_erasure_reset_prune(scratch_mongo), loop_factory=asyncio.SelectorEventLoop)


def test_mongo_records_projection_disabled_pruned_refusal(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_disabled_pruned_refusal(scratch_mongo), loop_factory=asyncio.SelectorEventLoop
    )


def test_mongo_records_projection_concurrent_clients(scratch_mongo: _Scratch) -> None:
    asyncio.run(_case_concurrent_clients(scratch_mongo), loop_factory=asyncio.SelectorEventLoop)


def test_mongo_records_projection_runtime_conflict_and_erasure(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_runtime_conflict_and_erasure(scratch_mongo), loop_factory=asyncio.SelectorEventLoop
    )


def test_mongo_records_projection_reset_wins_prune(
    scratch_mongo: _Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _case_reset_wins_prune(scratch_mongo, monkeypatch),
        loop_factory=asyncio.SelectorEventLoop,
    )


def test_mongo_records_projection_prune_wins_reset(
    scratch_mongo: _Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _case_prune_wins_reset(scratch_mongo, monkeypatch),
        loop_factory=asyncio.SelectorEventLoop,
    )


def test_mongo_records_projection_append_fence_and_reset_replay(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_append_fence_and_reset_replay(scratch_mongo),
        loop_factory=asyncio.SelectorEventLoop,
    )


def test_mongo_records_projection_foreign_erasure_refusal(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_foreign_erasure_refusal(scratch_mongo), loop_factory=asyncio.SelectorEventLoop
    )


def test_mongo_records_projection_foreign_mutators_and_snapshot(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_foreign_mutators_and_snapshot(scratch_mongo),
        loop_factory=asyncio.SelectorEventLoop,
    )


def test_mongo_records_projection_terminal_payload_validation(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_terminal_payload_validation(scratch_mongo), loop_factory=asyncio.SelectorEventLoop
    )


def test_mongo_records_projection_peer_visibility_snapshot(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_peer_visibility_snapshot(scratch_mongo), loop_factory=asyncio.SelectorEventLoop
    )


def test_mongo_records_projection_untyped_incumbent_lookup(
    scratch_mongo: _Scratch, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _case_untyped_incumbent_lookup(scratch_mongo, monkeypatch),
        loop_factory=asyncio.SelectorEventLoop,
    )


@pytest.mark.parametrize("hard_first", [True, False])
def test_mongo_records_projection_concurrent_fence_order(
    scratch_mongo: _Scratch, hard_first: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _case_concurrent_fence_order(scratch_mongo, hard_first=hard_first, monkeypatch=monkeypatch),
        loop_factory=asyncio.SelectorEventLoop,
    )


async def _case_marker_scrub_excludes_write(scratch: _Scratch) -> None:
    store, connector = await _open(scratch)
    try:
        record = _record("marker-write")
        marker = await store.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={"marker": "card", "text": "secret"},
            )
        )
        write = await store.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                payload={
                    "record": record.model_dump(mode="json"),
                    "marker": "card",
                    "text": "secret",
                },
            )
        )

        def scrub(payload: dict[str, Any]) -> bool:
            if payload.get("text") != "secret":
                return False
            payload["text"] = ""
            return True

        assert await store.scrub_marker_payloads("card", scrub, namespace="a") == [marker.seq]
        events = {event.seq: event for event in await store.read_events()}
        assert events[marker.seq].payload["text"] == ""
        assert events[write.seq].payload == write.payload
    finally:
        await store.stop()
        await connector.close()


def test_mongo_marker_scrub_excludes_write(scratch_mongo: _Scratch) -> None:
    asyncio.run(
        _case_marker_scrub_excludes_write(scratch_mongo), loop_factory=asyncio.SelectorEventLoop
    )


async def _case_registry_source_fence_race(
    scratch: _Scratch, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    first, first_connector = await _open(scratch)
    second, second_connector = await _open(scratch)
    try:
        await first.upsert_record(
            MemoryRecord(
                record_id="source",
                namespace="registry/native",
                memory_type="episodic",
                content="raw",
            )
        )
        marker = MemoryEvent(
            kind=EventKind.MARKER,
            namespace="registry/native",
            actor="resolver",
            payload={
                "marker": "entity.create",
                "scope": ["registry", "native"],
                "entity": "opaque",
                "name": "Private Native Name",
                "source_ids": ["source"],
                "source_versions": [1],
            },
        )
        entered, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_transaction = first._transaction
        original_next = second._next
        held_once = False

        async def hold_before_commit(body: Any) -> Any:
            async def wrapped(session: Any) -> Any:
                nonlocal held_once
                result = await body(session)
                if not held_once:
                    held_once = True
                    entered.set()  # source guard and marker INSERT, or fence INSERT completed
                    await asyncio.wait_for(release.wait(), 8)
                return result

            return await original_transaction(wrapped)

        monkeypatch.setattr(first, "_transaction", hold_before_commit)

        async def signal_native_counter_attempt(
            counter: Any, session: Any = None, by: int = 1
        ) -> int:
            if counter == "events" and session is not None:
                attempted.set()  # inside contender transaction, before counter document write
            return await original_next(counter, session, by)

        monkeypatch.setattr(second, "_next", signal_native_counter_attempt)
        leading = asyncio.create_task(
            first.append_registry_marker(marker)
            if winner == "marker"
            else first.begin_registry_erasure("registry/native", ["source"])
        )
        trailing: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 8)
            trailing = asyncio.create_task(
                second.begin_registry_erasure("registry/native", ["source"])
                if winner == "marker"
                else second.append_registry_marker(marker)
            )
            await asyncio.wait_for(attempted.wait(), 8)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(trailing), 0.5)
        finally:
            release.set()
            await asyncio.wait_for(leading, 8)
            if trailing is not None:
                if winner == "marker":
                    await asyncio.wait_for(trailing, 8)
                else:
                    with pytest.raises(StorageError, match="being erased"):
                        await asyncio.wait_for(trailing, 8)
        monkeypatch.setattr(first, "_transaction", original_transaction)
        monkeypatch.setattr(second, "_next", original_next)
        if winner == "marker":
            assert await second.scrub_marker_payloads(
                "entity.create",
                lambda payload: scrub_registry_marker(payload, {"source"}),
                namespace="registry/native",
            )
        events = await second.read_events()
        assert not any(event.payload.get("name") == "Private Native Name" for event in events)
        assert any(event.kind is EventKind.REGISTRY_ERASURE_FENCE for event in events)
        third, third_connector = await _open(scratch)
        try:
            with pytest.raises(StorageError, match="being erased"):
                await third.append_registry_marker(
                    MemoryEvent(
                        kind=marker.kind,
                        namespace=marker.namespace,
                        actor=marker.actor,
                        payload=marker.payload,
                    )
                )
        finally:
            await third.stop()
            await third_connector.close()
    finally:
        await first.stop()
        await second.stop()
        await first_connector.close()
        await second_connector.close()


@pytest.mark.parametrize("winner", ["marker", "fence"])
def test_mongo_registry_source_fence_race(
    scratch_mongo: _Scratch, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    asyncio.run(
        _case_registry_source_fence_race(scratch_mongo, monkeypatch, winner),
        loop_factory=asyncio.SelectorEventLoop,
    )


async def _case_registry_invalid_pin(scratch: _Scratch, change: str) -> None:
    store, connector = await _open(scratch)
    try:
        if change != "missing":
            await store.upsert_record(
                MemoryRecord(
                    record_id="source",
                    namespace="other/ns" if change == "foreign" else "registry/native",
                    memory_type="episodic",
                    content="raw",
                    version=2 if change == "stale" else 1,
                )
            )
        if change == "fenced":
            await store.begin_registry_erasure("registry/native", ["source"])
        with pytest.raises(StorageError, match="source changed|being erased"):
            await store.append_registry_marker(
                MemoryEvent(
                    kind=EventKind.MARKER,
                    namespace="registry/native",
                    actor="resolver",
                    payload={
                        "marker": "entity.create",
                        "scope": ["registry", "native"],
                        "entity": "opaque",
                        "name": "Private Native Name",
                        "source_ids": ["source"],
                        "source_versions": [1],
                    },
                )
            )
        assert not any(
            event.payload.get("name") == "Private Native Name"
            for event in await store.read_events()
        )
    finally:
        await store.stop()
        await connector.close()


@pytest.mark.parametrize("change", ["missing", "foreign", "stale", "fenced"])
def test_mongo_registry_rejects_invalid_source_pin(scratch_mongo: _Scratch, change: str) -> None:
    asyncio.run(
        _case_registry_invalid_pin(scratch_mongo, change), loop_factory=asyncio.SelectorEventLoop
    )
