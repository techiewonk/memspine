"""Owned PostgreSQL proof for the native transactional entity-index projector."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncConnection

from memspine.clients.postgres import PostgresClient, PostgresSpec
from memspine.exceptions import RebuildUnavailableError, StorageError
from memspine.services.storage.entity_index_tables import EntityIndexRow
from memspine.services.storage.postgres.engine import PostgresStorage
from memspine.services.storage.records_port import EntityIndexEffects, EntityIndexProjectionView
from memspine.services.storage.sqlite.schema import memory_events
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord

psycopg = pytest.importorskip("psycopg")


def _write(namespace: str, *, ts: datetime | None = None) -> MemoryEvent:
    record = MemoryRecord(
        record_id=uuid.uuid4().hex,
        namespace=namespace,
        memory_type="semantic",
        content="test",
    )
    event = MemoryEvent(
        kind=EventKind.WRITE,
        namespace=namespace,
        payload={"record": record.model_dump(mode="json")},
    )
    return event.model_copy(update={"ts": ts}) if ts is not None else event


@dataclass(frozen=True)
class _Target:
    url: str = field(repr=False)


def _owned_state() -> tuple[int, str, str, str]:
    path = Path(__file__).resolve().parents[2] / "scripts" / "local_services.py"
    spec = importlib.util.spec_from_file_location("local_services", path)
    assert spec is not None and spec.loader is not None
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    state = launcher.load_state()
    if state is None:
        pytest.skip("owned RM-136 PostgreSQL stack is absent")
    ids = launcher.owned_containers(state)
    launcher.owned_resources(state)
    if len(ids) != len(launcher.SERVICES):
        raise RuntimeError("owned service stack is incomplete")
    project, owner = str(state["project"]), str(state["owner"])
    port = launcher.ports_for(state)["POSTGRES"]
    pg_ids = launcher.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label=com.docker.compose.project={project}",
            "--filter",
            "label=com.docker.compose.service=postgres",
            "--format",
            "{{.ID}}",
        ]
    ).stdout.split()
    if len(pg_ids) != 1 or pg_ids[0] not in ids:
        raise RuntimeError("owned PostgreSQL container is missing or ambiguous")
    detail = launcher.run(["docker", "inspect", pg_ids[0]]).stdout
    inspected = json.loads(detail)
    if len(inspected) != 1:
        raise RuntimeError("owned PostgreSQL inspection returned an unexpected container")
    container = inspected[0]
    labels = container.get("Config", {}).get("Labels", {})
    bound = container.get("NetworkSettings", {}).get("Ports", {}).get("5432/tcp")
    if (
        labels.get("com.docker.compose.project") != project
        or labels.get("com.docker.compose.service") != "postgres"
        or labels.get("memspine.rm136.owner") != owner
        or container.get("State", {}).get("Status") != "running"
        or bound != [{"HostIp": "127.0.0.1", "HostPort": str(port)}]
    ):
        raise RuntimeError("owned PostgreSQL container does not match its loopback port")
    return (
        port,
        str(state["password"]),
        project,
        owner,
    )


@pytest.fixture
def scratch_postgres() -> Iterator[_Target]:
    port, password, project, owner = _owned_state()
    name = f"rm34_projection_{uuid.uuid4().hex[:10]}"
    with psycopg.connect(
        host="127.0.0.1",
        port=port,
        user="memspine_local",
        password=password,
        dbname="postgres",
        connect_timeout=3,
        autocommit=True,
    ) as conn:
        conn.execute(f'CREATE DATABASE "{name}"')
    try:
        yield _Target(f"postgresql+psycopg://memspine_local:{password}@127.0.0.1:{port}/{name}")
    finally:
        current_port, current_password, current_project, current_owner = _owned_state()
        if (current_port, current_project, current_owner) != (port, project, owner):
            raise RuntimeError("owned PostgreSQL target changed during scratch database lifetime")
        with psycopg.connect(
            host="127.0.0.1",
            port=port,
            user="memspine_local",
            password=current_password,
            dbname="postgres",
            connect_timeout=3,
            autocommit=True,
        ) as conn:
            conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')


async def _apply(
    view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
) -> EntityIndexEffects:
    for event in events:
        assert event.seq is not None
        row = EntityIndexRow(
            event.namespace, "entity", str(event.seq), str(event.seq), seq=event.seq
        )
        await view.put([row])
        assert await view.select(event.namespace, "entity", key=str(event.seq)) == [row]
        assert await view.get_record("absent") is None
    return EntityIndexEffects(changed=bool(events))


async def _case(target: _Target) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[0].initialize_entity_index()
        foreign = await stores[0].append_event(_write("b"))
        own = await stores[0].append_event(_write("a"))
        assert foreign.seq is not None and own.seq is not None
        a = await stores[0].advance_entity_index("a", own.seq, 1, _apply)
        assert a.global_offset < foreign.seq
        b = await stores[1].advance_entity_index("b", own.seq, 1, _apply)
        assert b.global_offset >= own.seq
        assert await stores[0].entity_index_revision("a") == (0, 1)

        async def fail(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            await view.delete("a", "entity", key=str(own.seq))
            raise RuntimeError("rollback")

        later = await stores[0].append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="a",
                payload={"record_id": uuid.uuid4().hex},
            )
        )
        assert later.seq is not None
        with pytest.raises(RuntimeError, match="rollback"):
            await stores[0].advance_entity_index("a", later.seq, 1, fail)
        assert await stores[0].entity_index_store().select("a", "entity", key=str(own.seq))
        assert await stores[1].entity_index_revision("a") == (0, 1)
        reset = await stores[1].reset_entity_index()
        assert (reset.epoch, reset.global_offset) == (1, 0)
        assert await stores[0].get_offset("entity_index") == 0
        assert await stores[0].entity_index_revision("a") == (1, 0)
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


def test_native_postgres_projection(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_case(scratch_postgres))


async def _concurrent_case(target: _Target) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[0].initialize_entity_index()
        a = await stores[0].append_event(_write("a"))
        b = await stores[0].append_event(_write("b"))
        assert a.seq is not None and b.seq is not None
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            entered.set()
            await release.wait()
            return await _apply(view, events)

        first = asyncio.create_task(stores[0].advance_entity_index("a", a.seq, 1, hold))
        await asyncio.wait_for(entered.wait(), 3)
        # Independent namespaces have separate advisory locks. B can commit even
        # though A's callback still holds the shared reset gate.
        other = await asyncio.wait_for(stores[1].advance_entity_index("b", b.seq, 1, _apply), 3)
        assert other.namespace_cursor == b.seq
        reset = asyncio.create_task(stores[1].reset_entity_index())
        await asyncio.sleep(0.1)
        assert not reset.done()
        release.set()
        prior = await asyncio.wait_for(first, 3)
        after = await asyncio.wait_for(reset, 3)
        assert prior.epoch == 0 and after.epoch == 1
        assert await stores[0].entity_index_store().select("a", "entity") == []
        assert await stores[1].get_offset("entity_index") == 0
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


def test_native_postgres_cross_namespace_and_reset_gate(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_concurrent_case(scratch_postgres))


async def _gap_case(target: _Target) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client, mode=EventLogMode.ROLLING)
    try:
        await client.open()
        await store.start()
        with pytest.raises(RuntimeError, match="intentional rollback"):
            async with client.engine.begin() as conn:
                await store._guard_event_append(conn)
                await conn.execute(store._event_insert(_write("a")))
                raise RuntimeError("intentional rollback")
        event = await store.append_event(_write("a"))
        assert event.seq is not None and event.seq > 1
        await store.assert_full_event_history()
        await store.initialize_entity_index()
        receipt = await store.advance_entity_index("a", event.seq, 1, _apply)
        assert (receipt.namespace_cursor, receipt.global_offset) == (event.seq, event.seq)
        with pytest.raises(StorageError, match="owns its ordered checkpoint"):
            await store.set_offset("entity_index", event.seq + 1000)
    finally:
        await store.stop()
        await client.close()


def test_native_postgres_rollback_gap_is_not_pruned_history(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_gap_case(scratch_postgres))


async def _late_enable_case(target: _Target) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client, mode=EventLogMode.ROLLING)
    try:
        await client.open()
        await store.start()
        old = datetime.now(UTC) - timedelta(days=2)
        event = await store.append_event(_write("a", ts=old))
        assert event.seq is not None
        await store.set_offset("other_projector", event.seq)
        assert await store.prune_events(datetime.now(UTC)) == 1
        assert await store.read_events() == []
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.assert_full_event_history()
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.initialize_entity_index()
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.advance_entity_index("a", event.seq, 1, _apply)
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.reset_entity_index()
    finally:
        await store.stop()
        await client.close()


def test_native_postgres_first_enable_refuses_fully_pruned_log(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_late_enable_case(scratch_postgres))


async def _enable_prune_race_case(
    target: _Target, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client, mode=EventLogMode.ROLLING) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        old = datetime.now(UTC) - timedelta(days=2)
        event = await stores[0].append_event(_write("a", ts=old))
        assert event.seq is not None
        await stores[0].set_offset("other_projector", event.seq)
        entered, release = asyncio.Event(), asyncio.Event()
        if winner == "initialize":
            original_register = stores[0]._entity_register

            async def held_register(conn: AsyncConnection) -> int:
                entered.set()
                await release.wait()
                return await original_register(conn)

            monkeypatch.setattr(stores[0], "_entity_register", held_register)
            first = asyncio.create_task(stores[0].initialize_entity_index())
            await asyncio.wait_for(entered.wait(), 3)
            second = asyncio.create_task(stores[1].prune_events(datetime.now(UTC)))
            await asyncio.sleep(0.1)
            assert not second.done()
            release.set()
            await asyncio.wait_for(first, 3)
            assert await asyncio.wait_for(second, 3) == 0
            assert len(await stores[0].read_events()) == 1
        else:
            original_guard = stores[0]._guard_event_append

            async def held_guard(conn: AsyncConnection) -> None:
                entered.set()
                await release.wait()
                await original_guard(conn)

            monkeypatch.setattr(stores[0], "_guard_event_append", held_guard)
            first_prune = asyncio.create_task(stores[0].prune_events(datetime.now(UTC)))
            await asyncio.wait_for(entered.wait(), 3)
            second_init = asyncio.create_task(stores[1].initialize_entity_index())
            await asyncio.sleep(0.1)
            assert not second_init.done()
            release.set()
            assert await asyncio.wait_for(first_prune, 3) == 1
            with pytest.raises(RebuildUnavailableError, match="history was pruned"):
                await asyncio.wait_for(second_init, 3)
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


@pytest.mark.parametrize("winner", ["initialize", "prune"])
def test_native_postgres_first_enable_prune_gate(
    scratch_postgres: _Target, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _enable_prune_race_case(scratch_postgres, monkeypatch, winner)
    )


async def _pending_case(target: _Target) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client)
    try:
        await client.open()
        await store.start()
        await store.initialize_entity_index()
        marker_event = await store.append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={"marker": "entity_index.merge", "merge_id": "opaque-id"},
            )
        )
        later_event = await store.append_event(_write("a"))
        assert marker_event.seq is not None and later_event.seq is not None
        marker, later = marker_event.seq, later_event.seq
        seen: list[list[int]] = []

        async def defer(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            seen.append([int(event.seq or 0) for event in events])
            with pytest.raises(StorageError, match="crossed its namespace"):
                await view.select("b", "entity")
            pending = await view.pending_events(1)
            if pending:
                assert [(event.seq, event.event_id) for event in pending] == [
                    (marker, marker_event.event_id)
                ]
            if events and events[0].seq == marker:
                await view.put(
                    [
                        EntityIndexRow(
                            "a", "pending_positive", str(marker), "opaque-id", "merge", seq=marker
                        )
                    ]
                )
            elif not events:
                await view.delete("a", "pending_positive", key=str(marker))
            return EntityIndexEffects(changed=True)

        first = await store.advance_entity_index("a", marker, 1, defer)
        assert (first.namespace_cursor, first.complete, first.global_offset) == (0, False, 0)
        second = await store.advance_entity_index("a", later, 1, defer)
        assert (second.namespace_cursor, second.complete, second.global_offset) == (0, False, 0)
        third = await store.advance_entity_index("a", later, 1, defer)
        assert (third.namespace_cursor, third.complete, third.applied_count) == (later, True, 0)
        assert third.global_offset >= later
        assert seen == [[marker], [later], []]

        missing_event = await store.append_event(
            MemoryEvent(
                kind=EventKind.MARKER, namespace="a", payload={"marker": "entity_index.alias"}
            )
        )
        assert missing_event.seq is not None

        async def pend_missing(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            await view.put(
                [
                    EntityIndexRow(
                        "a",
                        "pending_positive",
                        str(missing_event.seq),
                        "opaque-alias",
                        "alias",
                        seq=missing_event.seq or 0,
                    )
                ]
            )
            return EntityIndexEffects(changed=True)

        pending_receipt = await store.advance_entity_index("a", missing_event.seq, 1, pend_missing)
        assert pending_receipt.namespace_cursor == later
        async with client.engine.begin() as conn:
            await conn.execute(
                delete(memory_events).where(memory_events.c.seq == missing_event.seq)
            )

        async def retry_missing(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            assert events == []
            await view.pending_events(1)
            return EntityIndexEffects()

        with pytest.raises(StorageError, match="canonical event is missing"):
            await store.advance_entity_index("a", missing_event.seq, 1, retry_missing)
        assert await store.entity_index_store().select("a", "pending_positive")
        assert await store.get_offset("entity_index") == later
    finally:
        await store.stop()
        await client.close()


def test_native_postgres_pending_marker_certification(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_pending_case(scratch_postgres))


async def _purge_prefix_case(target: _Target) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client)
    try:
        await client.open()
        await store.start()
        await store.initialize_entity_index()

        async def pending(
            view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
        ) -> EntityIndexEffects:
            assert len(events) == 1
            event = events[0]
            seq = event.seq
            assert seq is not None
            await view.put(
                [
                    EntityIndexRow(
                        event.namespace,
                        "pending_positive",
                        str(seq),
                        "id",
                        "alias",
                        seq=seq,
                    )
                ]
            )
            return EntityIndexEffects(changed=True)

        async def purge(view: EntityIndexProjectionView, ids: Sequence[str]) -> EntityIndexEffects:
            assert ids == ["evidence"]
            rows = await view.select(current_namespace, "pending_positive")
            assert len(rows) == 1
            await view.delete(current_namespace, "pending_positive", key=rows[0].key)
            return EntityIndexEffects(changed=True)

        current_namespace = "a"
        first = await store.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a"))
        assert first.seq is not None
        assert (await store.advance_entity_index("a", first.seq, 1, pending)).global_offset == 0
        released = await store.purge_entity_index("a", ["evidence"], purge)
        assert (released.namespace_cursor, released.global_offset) == (first.seq, first.seq)
        assert await store.get_offset("entity_index") == first.seq

        foreign = await store.append_event(_write("b"))
        current_namespace = "c"
        own = await store.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="c"))
        assert foreign.seq is not None and own.seq is not None
        blocked = await store.advance_entity_index("c", own.seq, 1, pending)
        assert blocked.global_offset == first.seq
        released = await store.purge_entity_index("c", ["evidence"], purge)
        assert released.namespace_cursor == own.seq
        assert released.global_offset == first.seq
        assert await store.get_offset("entity_index") == first.seq
    finally:
        await store.stop()
        await client.close()


def test_native_postgres_purge_releases_only_covered_global_prefix(
    scratch_postgres: _Target,
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_purge_prefix_case(scratch_postgres))
