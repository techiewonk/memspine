"""Owned PostgreSQL proof of the ordered records fold and ephemeral door."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import uuid
from collections.abc import AsyncIterator, Iterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

import memspine.services.storage.sql_base as sql_base
from memspine.clients.postgres import PostgresClient, PostgresSpec
from memspine.exceptions import RebuildUnavailableError, StorageError
from memspine.schemas.durable.common import OwnerScope
from memspine.services.storage.postgres.engine import PostgresStorage
from memspine.services.storage.projector import fold_records
from memspine.services.storage.records_port import OutboxItem, RecordsProjectionView
from memspine.store.claim_cardinality import ClaimGroup, group_key
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord
from memspine.store.registry_evidence import scrub_registry_marker

psycopg = pytest.importorskip("psycopg")


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
        pytest.skip("owned PostgreSQL stack is absent")
    ids = launcher.owned_containers(state)
    launcher.owned_resources(state)
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
    inspected = json.loads(launcher.run(["docker", "inspect", pg_ids[0]]).stdout)
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
    return port, str(state["password"]), project, owner


@pytest.fixture
def scratch_postgres() -> Iterator[_Target]:
    port, password, project, owner = _owned_state()
    name = f"rm45_records_{uuid.uuid4().hex[:10]}"
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


def _record() -> MemoryRecord:
    return MemoryRecord(record_id="record-a", namespace="a", memory_type="semantic", content="old")


async def _ordered_case(target: _Target) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[0].initialize_records_projection()
        record = _record()
        write = await stores[0].append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                payload={"record": record.model_dump(mode="json")},
            )
        )
        assert write.seq is not None
        first = await stores[1].advance_records(write.seq, 1, fold_records)
        assert (first.cursor, first.applied_count, first.complete) == (write.seq, 1, True)
        assert await stores[0].get_offset("records") == write.seq
        assert await stores[1].get_record(record.record_id) is not None
        forget = await stores[0].append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="a",
                payload={"record_id": record.record_id, "hard": True},
            )
        )
        assert forget.seq is not None

        async def fail(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await fold_records(view, events)
            raise RuntimeError("rollback")

        with pytest.raises(RuntimeError, match="rollback"):
            await stores[0].advance_records(forget.seq, 1, fail)
        assert await stores[1].get_record(record.record_id) is not None
        assert await stores[0].get_offset("records") == write.seq
        last = await stores[1].advance_records(forget.seq, 1, fold_records)
        assert (last.cursor, last.complete) == (forget.seq, True)
        assert await stores[0].get_record(record.record_id) is None
        with pytest.raises(StorageError, match="owns its ordered checkpoint"):
            await stores[0].set_offset("records", forget.seq + 100)
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


def test_native_postgres_records_fold(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_ordered_case(scratch_postgres))


async def _ephemeral_case(target: _Target) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client, mode=EventLogMode.EPHEMERAL)
    try:
        await client.open()
        await store.start()
        record = _record()
        write = MemoryEvent(
            kind=EventKind.WRITE, namespace="a", payload={"record": record.model_dump(mode="json")}
        )
        with pytest.raises(StorageError, match="append_ephemeral_records"):
            await store.append_event(write)

        async def cancel(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await fold_records(view, events)
            raise asyncio.CancelledError

        with pytest.raises(asyncio.CancelledError):
            await store.append_ephemeral_records(write, cancel)
        assert await store.get_record(record.record_id) is None
        projected = await store.append_ephemeral_records(write, fold_records)
        assert projected.seq == 1
        assert await store.get_record(record.record_id) is not None
        hard = MemoryEvent(
            kind=EventKind.FORGET,
            namespace="a",
            payload={"record_id": record.record_id, "hard": True},
        )
        with pytest.raises(asyncio.CancelledError):
            await store.append_ephemeral_records(hard, cancel)
        assert await store.get_record(record.record_id) is not None
        rewritten = await store.append_ephemeral_records(
            MemoryEvent(kind=EventKind.WRITE, namespace="a", payload=write.payload), fold_records
        )
        assert rewritten.seq == 2
        erased = await store.append_ephemeral_records(hard, fold_records)
        assert erased.seq == 3
        assert await store.get_record(record.record_id) is None
        with pytest.raises(StorageError, match="permanently erased"):
            await store.append_ephemeral_records(
                MemoryEvent(kind=EventKind.WRITE, namespace="a", payload=write.payload),
                fold_records,
            )
        assert await store.read_events() == []
        assert await store.get_offset("records") == 0
    finally:
        await store.stop()
        await client.close()


def test_native_postgres_ephemeral_records_door(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_ephemeral_case(scratch_postgres))


async def _reset_race_case(target: _Target) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[0].initialize_records_projection()
        event = await stores[0].append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a"))
        assert event.seq is not None
        entered, release = asyncio.Event(), asyncio.Event()

        async def hold(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            entered.set()
            await release.wait()
            await fold_records(view, events)

        advancing = asyncio.create_task(stores[0].advance_records(event.seq, 1, hold))
        await asyncio.wait_for(entered.wait(), 3)
        resetting = asyncio.create_task(stores[1].reset_records_projection())
        await asyncio.sleep(0.1)
        assert not resetting.done()
        release.set()
        assert (await asyncio.wait_for(advancing, 3)).cursor == event.seq
        await asyncio.wait_for(resetting, 3)
        assert await stores[0].get_offset("records") == 0
        assert (await stores[1].advance_records(event.seq, 1, fold_records)).cursor == event.seq
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


def test_native_postgres_reset_waits_for_records_fold(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_reset_race_case(scratch_postgres))


async def _registration_prune_case(
    target: _Target, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client, mode=EventLogMode.ROLLING) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        old = datetime.now(UTC) - timedelta(days=2)
        event = await stores[0].append_event(
            MemoryEvent(kind=EventKind.MARKER, namespace="a", ts=old)
        )
        assert event.seq is not None
        await stores[0].set_offset("other_projector", event.seq)
        entered, release = asyncio.Event(), asyncio.Event()
        if winner == "registration":
            original_register = stores[0]._records_register

            async def held_register(conn: AsyncConnection) -> int:
                entered.set()
                await release.wait()
                return await original_register(conn)

            monkeypatch.setattr(stores[0], "_records_register", held_register)
            registering = asyncio.create_task(stores[0].initialize_records_projection())
            await asyncio.wait_for(entered.wait(), 3)
            pruning = asyncio.create_task(stores[1].prune_events(datetime.now(UTC)))
            await asyncio.sleep(0.1)
            assert not pruning.done()
            release.set()
            await asyncio.wait_for(registering, 3)
            assert await asyncio.wait_for(pruning, 3) == 0
        else:
            original_append_guard = stores[0]._guard_event_append

            async def held_append_guard(conn: AsyncConnection) -> None:
                entered.set()
                await release.wait()
                await original_append_guard(conn)

            monkeypatch.setattr(stores[0], "_guard_event_append", held_append_guard)
            pruning = asyncio.create_task(stores[0].prune_events(datetime.now(UTC)))
            await asyncio.wait_for(entered.wait(), 3)
            registering = asyncio.create_task(stores[1].initialize_records_projection())
            await asyncio.sleep(0.1)
            assert not registering.done()
            release.set()
            assert await asyncio.wait_for(pruning, 3) == 1
            with pytest.raises(RebuildUnavailableError, match="history was pruned"):
                await asyncio.wait_for(registering, 3)
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


@pytest.mark.parametrize("winner", ["registration", "prune"])
def test_native_postgres_records_registration_prune_gate(
    scratch_postgres: _Target, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _registration_prune_case(scratch_postgres, monkeypatch, winner)
    )


async def _fence_order_case(target: _Target, monkeypatch: pytest.MonkeyPatch, winner: str) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[0].initialize_records_projection()
        record = _record()
        write = MemoryEvent(
            kind=EventKind.WRITE,
            namespace="a",
            payload={"record": record.model_dump(mode="json")},
        )
        hard = MemoryEvent(
            kind=EventKind.FORGET,
            namespace="a" if winner != "write" else "b",
            payload={"record_id": record.record_id, "hard": True},
        )
        first_event, second_event = (write, hard) if winner == "write" else (hard, write)
        entered, release = asyncio.Event(), asyncio.Event()
        original_fence = stores[0]._record_append_fence_on

        async def held_fence(
            conn: AsyncConnection, event: MemoryEvent, *, trusted_replay: bool = False
        ) -> None:
            await original_fence(conn, event, trusted_replay=trusted_replay)
            entered.set()
            await release.wait()

        monkeypatch.setattr(stores[0], "_record_append_fence_on", held_fence)
        first = asyncio.create_task(stores[0].append_event(first_event))
        await asyncio.wait_for(entered.wait(), 3)
        second = asyncio.create_task(stores[1].append_event(second_event))
        await asyncio.sleep(0.1)
        assert not second.done()  # global append guard orders the competing transactions
        if winner == "cancel":
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            release.set()
            succeeded = await asyncio.wait_for(second, 3)
            assert succeeded.kind is EventKind.WRITE
        else:
            release.set()
            await asyncio.wait_for(first, 3)
            with pytest.raises(StorageError, match="another namespace|permanently erased"):
                await asyncio.wait_for(second, 3)
        assert len(await stores[0].read_events()) == 1
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


@pytest.mark.parametrize("winner", ["hard", "write", "cancel"])
def test_native_postgres_append_erasure_fence_ordering(
    scratch_postgres: _Target, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _fence_order_case(scratch_postgres, monkeypatch, winner)
    )


async def _fence_persistence_case(target: _Target, rolling: bool) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client, mode=EventLogMode.ROLLING if rolling else EventLogMode.FULL)
    peer_client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    peer = PostgresStorage(peer_client, mode=store.mode)
    try:
        await client.open()
        await store.start()
        await store.initialize_records_projection()
        record = _record()
        old = datetime.now(UTC) - timedelta(days=2)
        write = await store.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                ts=old,
                payload={"record": record.model_dump(mode="json")},
            )
        )
        hard = await store.append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="a",
                ts=old,
                payload={"record_id": record.record_id, "hard": True},
            )
        )
        assert write.seq is not None and hard.seq is not None
        assert (await store.advance_records(hard.seq, 2, fold_records)).complete
        assert await store.get_record(record.record_id) is None
        if rolling:
            assert await store.prune_events(datetime.now(UTC)) == 2
            assert await store.read_events() == []
            with pytest.raises(RebuildUnavailableError, match="history was pruned"):
                await store.reset_records_projection()
        else:
            await store.reset_records_projection()
            assert await store.get_offset("records") == 0
        await peer_client.open()
        await peer.start()  # independent connector after reset/prune
        fresh = MemoryEvent(
            kind=EventKind.WRITE,
            namespace="a",
            payload={"record": record.model_dump(mode="json")},
        )
        scope = OwnerScope(tenant="tenant", owner="owner")
        with pytest.raises(StorageError, match="permanently erased"):
            await peer.append_with_outbox(fresh, [OutboxItem(scope, "stage", {"id": "job"})])
        assert await peer.outbox_items(scope, "stage") == []
        with pytest.raises(StorageError, match="permanently erased"):
            await peer.append_event(fresh)
    finally:
        await peer.stop()
        await peer_client.close()
        await store.stop()
        await client.close()


@pytest.mark.parametrize("rolling", [False, True])
def test_native_postgres_append_fence_survives_reset_or_prune(
    scratch_postgres: _Target, rolling: bool
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _fence_persistence_case(scratch_postgres, rolling)
    )


async def _foreign_identity_case(target: _Target) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        record = _record()
        await stores[0].append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                payload={"record": record.model_dump(mode="json")},
            )
        )
        for event in (
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="b",
                payload={"record_id": record.record_id},
            ),
            MemoryEvent(
                kind=EventKind.DECAY_TRANSITION,
                namespace="b",
                payload={"record_id": record.record_id, "set": {"tier": "cold"}},
            ),
        ):
            with pytest.raises(StorageError, match="another namespace"):
                await stores[1].append_event(event)
        with pytest.raises(StorageError, match="another namespace"):
            await stores[1].upsert_record(record.model_copy(update={"namespace": "b"}))
        for kind in (EventKind.FORGET, EventKind.DECAY_TRANSITION):
            with pytest.raises(StorageError, match="record_id"):
                await stores[1].append_event(MemoryEvent(kind=kind, namespace="b"))
        assert len(await stores[0].read_events()) == 1
        assert await stores[0].get_record(record.record_id) is None
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


def test_native_postgres_unprojected_identity_guards_all_doors(
    scratch_postgres: _Target,
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _foreign_identity_case(scratch_postgres)
    )


async def _first_erasure_owner_case(target: _Target, erasure_kind: str) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()

        def erase(namespace: str) -> MemoryEvent:
            if erasure_kind == "hard":
                return MemoryEvent(
                    kind=EventKind.FORGET,
                    namespace=namespace,
                    payload={"record_id": "never-written", "hard": True},
                )
            return MemoryEvent(
                kind=EventKind.MARKER,
                namespace=namespace,
                payload={
                    "marker": "erasure_manifest",
                    "manifest": {"ids": {"record_ids": ["never-written"]}},
                },
            )

        await stores[0].append_event(erase("a"))
        await stores[1].append_event(erase("a"))
        with pytest.raises(StorageError, match="another namespace"):
            await stores[1].append_event(erase("b"))
        assert len(await stores[0].read_events()) == 2
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


@pytest.mark.parametrize("erasure_kind", ["hard", "manifest"])
def test_native_postgres_first_erasure_claims_unknown_id(
    scratch_postgres: _Target, erasure_kind: str
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _first_erasure_owner_case(scratch_postgres, erasure_kind)
    )


async def _native_visibility_case(target: _Target, monkeypatch: pytest.MonkeyPatch) -> None:
    group = ClaimGroup("a", "tenant", "owner", "person", (), "subject", "born_in", "digest", 1, ())
    key = group_key(group)
    monkeypatch.setattr(sql_base, "record_group_of", lambda record: group)
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[0].initialize_records_projection()
        first = await stores[0].append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a"))
        assert first.seq is not None

        async def populate(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await view.put_record(_record())
            await view.put_record(_record().model_copy(update={"record_id": "record-b"}))

        await stores[0].advance_records(first.seq, 1, populate)
        assert len(await stores[1].claim_group_snapshot("a", key)) == 2
        hidden = await stores[0].append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={"marker": "erasure_manifest", "hidden": ["record-b"]},
            )
        )
        assert hidden.seq is not None

        async def hide(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await view.mark_hidden("a", "record-b")

        await stores[0].advance_records(hidden.seq, 1, hide)
        assert [r.record_id for r in await stores[1].claim_group_snapshot("a", key)] == ["record-a"]
        assert await stores[1].hidden_or_erased_record_ids("a", ["record-a", "record-b"]) == {
            "record-b"
        }
        released = await stores[0].append_event(
            MemoryEvent(
                kind=EventKind.MARKER,
                namespace="a",
                payload={"marker": "erasure_release", "record_ids": ["record-b"]},
            )
        )
        assert released.seq is not None

        async def release(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
            await view.release_hidden("a", "record-b")

        await stores[0].advance_records(released.seq, 1, release)
        assert len(await stores[1].claim_group_snapshot("a", key)) == 2
        assert await stores[1].hidden_or_erased_record_ids("a", ["record-b"]) == set()
        await stores[0].append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="a",
                payload={"record_id": "record-b", "hard": True},
            )
        )
        assert [r.record_id for r in await stores[1].claim_group_snapshot("a", key)] == ["record-a"]
        assert await stores[1].hidden_or_erased_record_ids("a", ["record-b"]) == {"record-b"}
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


def test_native_postgres_claim_snapshot_honors_peer_erasure(
    scratch_postgres: _Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _native_visibility_case(scratch_postgres, monkeypatch)
    )


async def _untyped_fact_case(target: _Target, monkeypatch: pytest.MonkeyPatch) -> None:
    group = ClaimGroup("a", "tenant", "owner", "person", (), "subject", "city", "digest", 1, ())
    monkeypatch.setattr(
        sql_base, "record_group_of", lambda record: group if record.record_id == "typed" else None
    )
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(2)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        now = datetime.now(UTC)
        base = _record().model_copy(update={"entity": "person", "attribute": "city"})
        for rid, content, ordinal in (
            ("berlin", "Berlin", 3),
            ("typed", "Lisbon", 2),
        ):
            await stores[0].upsert_record(
                base.model_copy(
                    update={
                        "record_id": rid,
                        "content": content,
                        "recorded_at": now - timedelta(ordinal),
                    }
                )
            )
        latest = await stores[1].find_active_fact("a", "person", "city")
        untyped = await stores[1].find_active_untyped_fact("a", "person", "city")
        assert latest is not None and latest.record_id == "typed"
        assert untyped is not None and untyped.record_id == "berlin"
        await stores[0].upsert_record(
            base.model_copy(update={"record_id": "porto", "content": "Porto", "recorded_at": now})
        )
        untyped = await stores[1].find_active_untyped_fact("a", "person", "city")
        assert untyped is not None and untyped.record_id == "porto"
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


def test_native_postgres_active_untyped_fact_excludes_typed_interloper(
    scratch_postgres: _Target, monkeypatch: pytest.MonkeyPatch
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _untyped_fact_case(scratch_postgres, monkeypatch)
    )


async def _marker_scrub_case(target: _Target) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client)
    try:
        await client.open()
        await store.start()
        record = _record()
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

        def scrub(payload: dict[str, object]) -> bool:
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
        await client.close()


def test_native_postgres_marker_scrub_excludes_write(scratch_postgres: _Target) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(_marker_scrub_case(scratch_postgres))


async def _registry_fence_race_case(
    target: _Target, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    clients = [PostgresClient(PostgresSpec(target.url, connect_timeout_s=3)) for _ in range(3)]
    stores = [PostgresStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[0].upsert_record(
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
        held = stores[0]
        contender = stores[1]
        entered, attempted, release = asyncio.Event(), asyncio.Event(), asyncio.Event()
        original_begin = AsyncEngine.begin
        original_execute = AsyncConnection.execute
        held_once = False

        @asynccontextmanager
        async def hold_before_commit(engine: AsyncEngine) -> AsyncIterator[AsyncConnection]:
            nonlocal held_once
            async with original_begin(engine) as conn:
                yield conn
                if engine is held._client.engine and not held_once:
                    held_once = True
                    entered.set()  # the source check or fence INSERT has completed
                    await asyncio.wait_for(release.wait(), 8)

        monkeypatch.setattr(AsyncEngine, "begin", hold_before_commit)

        async def signal_native_lock_attempt(
            conn: AsyncConnection, *args: Any, **kwargs: Any
        ) -> Any:
            statement = args[0] if args else kwargs.get("statement")
            parameters = args[1] if len(args) > 1 else kwargs.get("parameters")
            if (
                conn.engine is contender._client.engine
                and "pg_advisory_xact_lock" in str(statement)
                and isinstance(parameters, dict)
                and "key" in parameters
            ):
                attempted.set()  # inside contender transaction, immediately before native lock await
            return await original_execute(conn, *args, **kwargs)

        monkeypatch.setattr(AsyncConnection, "execute", signal_native_lock_attempt)
        first = asyncio.create_task(
            held.append_registry_marker(marker)
            if winner == "marker"
            else held.begin_registry_erasure("registry/native", ["source"])
        )
        second: asyncio.Task[Any] | None = None
        try:
            await asyncio.wait_for(entered.wait(), 8)
            second = asyncio.create_task(
                contender.begin_registry_erasure("registry/native", ["source"])
                if winner == "marker"
                else contender.append_registry_marker(marker)
            )
            await asyncio.wait_for(attempted.wait(), 8)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(second), 0.5)
        finally:
            release.set()
            await asyncio.wait_for(first, 8)
            if second is not None:
                if winner == "marker":
                    await asyncio.wait_for(second, 8)
                else:
                    with pytest.raises(StorageError, match="being erased"):
                        await asyncio.wait_for(second, 8)
        monkeypatch.setattr(AsyncEngine, "begin", original_begin)
        monkeypatch.setattr(AsyncConnection, "execute", original_execute)
        if winner == "marker":
            assert await contender.scrub_marker_payloads(
                "entity.create",
                lambda payload: scrub_registry_marker(payload, {"source"}),
                namespace="registry/native",
            )
        events = await contender.read_events()
        assert not any(event.payload.get("name") == "Private Native Name" for event in events)
        assert any(event.kind is EventKind.REGISTRY_ERASURE_FENCE for event in events)
        await clients[2].close()
        await clients[2].open()
        reopened = stores[2]
        with pytest.raises(StorageError, match="being erased"):
            await reopened.append_registry_marker(
                MemoryEvent(
                    kind=marker.kind,
                    namespace=marker.namespace,
                    actor=marker.actor,
                    payload=marker.payload,
                )
            )
    finally:
        for store in stores:
            await store.stop()
        for client in clients:
            await client.close()


@pytest.mark.parametrize("winner", ["marker", "fence"])
def test_native_postgres_registry_source_fence_race(
    scratch_postgres: _Target, monkeypatch: pytest.MonkeyPatch, winner: str
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _registry_fence_race_case(scratch_postgres, monkeypatch, winner)
    )


async def _registry_invalid_pin_case(target: _Target, change: str) -> None:
    client = PostgresClient(PostgresSpec(target.url, connect_timeout_s=3))
    store = PostgresStorage(client)
    try:
        await client.open()
        await store.start()
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
        await client.close()


@pytest.mark.parametrize("change", ["missing", "foreign", "stale", "fenced"])
def test_native_postgres_registry_rejects_invalid_source_pin(
    scratch_postgres: _Target, change: str
) -> None:
    asyncio.Runner(loop_factory=asyncio.SelectorEventLoop).run(
        _registry_invalid_pin_case(scratch_postgres, change)
    )
