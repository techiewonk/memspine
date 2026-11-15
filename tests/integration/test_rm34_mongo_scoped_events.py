"""RM-34: bounded scoped event reads on the owned local MongoDB replica set."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from memspine.clients.mongodb import MongoConnector, MongoSpec
from memspine.services.storage.entity_index_tables import EntityIndexRow
from memspine.services.storage.mongodb import MongoRecordsStore
from memspine.store.events import EventKind, MemoryEvent

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


@pytest.fixture
def scratch_mongo() -> Iterator[tuple[str, str]]:
    from pymongo import MongoClient

    port, project, owner = _owned_mongo()
    name = f"rm34_{uuid.uuid4().hex[:12]}"
    url = f"mongodb://127.0.0.1:{port}/?directConnection=true&replicaSet=rs0"
    try:
        yield url, name
    finally:
        _owned_mongo((project, owner, port))
        admin: MongoClient[dict[str, Any]] = MongoClient(url, serverSelectionTimeoutMS=3000)
        with admin as client:
            client.drop_database(name)


async def _open_store(url: str, name: str) -> tuple[MongoRecordsStore, MongoConnector]:
    connector = MongoConnector(MongoSpec(url, database=name, connect_timeout_s=3))
    await connector.open()
    store = MongoRecordsStore(connector)
    try:
        await store.start()
    except BaseException:
        await connector.close()
        raise
    return store, connector


async def _scoped_head_case(url: str, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    from pymongo.asynchronous.collection import AsyncCollection

    store, connector = await _open_store(url, name)
    try:
        for i in range(12):
            await store.append_event(
                MemoryEvent(
                    kind=EventKind.FORGET, namespace="foreign", payload={"record_id": str(i)}
                )
            )
        await store.append_event(
            MemoryEvent(kind=EventKind.MARKER, namespace="target", payload={"marker": "other"})
        )
        own = await store.append_event(
            MemoryEvent(kind=EventKind.FORGET, namespace="target", payload={"record_id": "own"})
        )
        tail = await store.append_event(
            MemoryEvent(kind=EventKind.FORGET, namespace="foreign", payload={"record_id": "tail"})
        )
        found, head = await store.read_scoped_events(
            "target", [EventKind.FORGET], after_seq=0, limit=1
        )
        assert [event.seq for event in found] == [own.seq]
        assert head == tail.seq  # global head, despite a namespace/kind filtered page

        original_find_one = AsyncCollection.find_one
        appended: MemoryEvent | None = None

        async def after_head(self: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal appended
            result = await original_find_one(self, *args, **kwargs)
            if self.name == "events" and args and args[0] == {} and appended is None:
                appended = await store.append_event(
                    MemoryEvent(
                        kind=EventKind.FORGET,
                        namespace="target",
                        payload={"record_id": "after-head"},
                    )
                )
            return result

        monkeypatch.setattr(AsyncCollection, "find_one", after_head)
        found, captured = await store.read_scoped_events(
            "target", [EventKind.FORGET], after_seq=0, limit=3
        )
        assert appended is not None and appended.seq is not None
        assert captured == tail.seq
        assert [event.seq for event in found] == [own.seq]
        later, new_head = await store.read_scoped_events(
            "target", [EventKind.FORGET], after_seq=captured, limit=1
        )
        assert [event.seq for event in later] == [appended.seq]
        assert new_head == appended.seq
    finally:
        await store.stop()
        await connector.close()


def test_mongo_scoped_filter_and_captured_head(
    scratch_mongo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _scoped_head_case(*scratch_mongo, monkeypatch), loop_factory=asyncio.SelectorEventLoop
    )


async def _counter_order_case(url: str, name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    first, first_connector = await _open_store(url, name)
    second, second_connector = await _open_store(url, name)
    allocated, release = asyncio.Event(), asyncio.Event()
    original_next = MongoRecordsStore._next

    async def held_next(self: MongoRecordsStore, counter: Any, *args: Any, **kwargs: Any) -> int:
        seq = await original_next(self, counter, *args, **kwargs)
        if self is first and counter == "events":
            allocated.set()
            await asyncio.wait_for(release.wait(), 8)
        return seq

    monkeypatch.setattr(MongoRecordsStore, "_next", held_next)
    task_a = asyncio.create_task(
        first.append_event(
            MemoryEvent(kind=EventKind.FORGET, namespace="target", payload={"record_id": "a"})
        )
    )
    task_b: asyncio.Task[MemoryEvent] | None = None
    try:
        await asyncio.wait_for(allocated.wait(), 8)
        task_b = asyncio.create_task(
            second.append_event(
                MemoryEvent(kind=EventKind.FORGET, namespace="target", payload={"record_id": "b"})
            )
        )
        try:
            await asyncio.wait_for(asyncio.shield(task_b), 0.75)
        except TimeoutError:
            pass
        assert not task_b.done()
        pending, head = await second.read_scoped_events(
            "target", [EventKind.FORGET], after_seq=0, limit=3
        )
        assert pending == [] and head == 0
    finally:
        release.set()
        await asyncio.wait_for(task_a, 8)
        if task_b is not None:
            await asyncio.wait_for(task_b, 8)
        await first.stop()
        await second.stop()
        await first_connector.close()
        await second_connector.close()
    assert task_b is not None
    a_seq, b_seq = task_a.result().seq, task_b.result().seq
    assert a_seq is not None and b_seq is not None
    assert a_seq < b_seq


def test_mongo_counter_transaction_orders_commits(
    scratch_mongo: tuple[str, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.run(
        _counter_order_case(*scratch_mongo, monkeypatch), loop_factory=asyncio.SelectorEventLoop
    )


async def _link_upsert_case(url: str, name: str) -> None:
    first, first_connector = await _open_store(url, name)
    second, second_connector = await _open_store(url, name)
    try:
        high = EntityIndexRow(
            "target",
            "merge",
            "same-link",
            "keeper",
            "newer",
            data={"link": True, "basis": "unique_name"},
            seq=4,
        )
        low = EntityIndexRow(
            "target",
            "merge",
            "same-link",
            "keeper",
            "newer",
            data={"link": True, "basis": "unique_name"},
            seq=3,
        )
        left, right = first.entity_index_store(), second.entity_index_store()
        await asyncio.gather(left.put([high]), right.put([low]))
        rows = await left.select("target", "merge", key="same-link")
        assert [row.seq for row in rows] == [3]
        await right.put([high])
        assert [row.seq for row in await left.select("target", "merge", key="same-link")] == [3]
        competing_late = EntityIndexRow(
            "target", "merge", "target-b", "other", "competing", data={"link": True}, seq=6
        )
        competing_early = EntityIndexRow(
            "target", "merge", "target-a", "keeper", "competing", data={"link": True}, seq=5
        )
        await asyncio.gather(left.put([competing_late]), right.put([competing_early]))
        assert [
            (row.key, row.seq) for row in await left.select("target", "merge", ref="competing")
        ] == [("target-a", 5)]
        cited = EntityIndexRow(
            "target", "merge", "cited", "keeper", "competing", data={"evidence": ["r"]}, seq=7
        )
        await right.put([cited])
        await left.put([competing_late])
        assert {
            (row.key, row.seq) for row in await left.select("target", "merge", ref="competing")
        } == {("target-a", 5), ("cited", 7)}
    finally:
        await first.stop()
        await second.stop()
        await first_connector.close()
        await second_connector.close()


def test_mongo_link_upsert_keeps_first_seq_across_independent_stores(
    scratch_mongo: tuple[str, str],
) -> None:
    asyncio.run(_link_upsert_case(*scratch_mongo), loop_factory=asyncio.SelectorEventLoop)
