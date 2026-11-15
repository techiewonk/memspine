"""SQLite records projection transaction and checkpoint conformance."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import memspine.services.storage.sql_base as sql_base
from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.exceptions import RebuildUnavailableError, StorageError
from memspine.schemas.durable.common import OwnerScope
from memspine.services.storage.projector import fold_records
from memspine.services.storage.records_port import OutboxItem, RecordsProjectionView
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.claim_cardinality import ClaimGroup, group_key
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord


@pytest.fixture(params=["memory", "file"])
async def storage(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[SQLiteStorage]:
    path = ":memory:" if request.param == "memory" else str(tmp_path / "records-projection.db")
    client = SQLiteClient(SQLiteSpec(path, busy_timeout_s=10.0))
    await client.open()
    store = SQLiteStorage(client)
    try:
        await store.start()
        await store.initialize_records_projection()
        yield store
    finally:
        await store.stop()
        await client.close()


def _record() -> MemoryRecord:
    return MemoryRecord(record_id="record-a", namespace="a", memory_type="semantic", content="old")


@pytest.mark.asyncio
async def test_ordered_all_event_page_and_atomic_checkpoint(storage: SQLiteStorage) -> None:
    record = _record()
    first = await storage.append_event(
        MemoryEvent(
            kind=EventKind.WRITE, namespace="a", payload={"record": record.model_dump(mode="json")}
        )
    )
    second = await storage.append_event(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace="a",
            payload={"record_id": record.record_id, "set": {"tier": "cold"}},
        )
    )
    assert first.seq is not None and second.seq is not None
    a = await storage.advance_records(second.seq, 1, fold_records)
    assert (a.cursor, a.applied_count, a.complete) == (first.seq, 1, False)
    assert await storage.get_offset("records") == first.seq
    assert await storage.get_record(record.record_id) is not None
    b = await storage.advance_records(second.seq, 1, fold_records)
    assert (b.cursor, b.applied_count, b.complete) == (second.seq, 1, True)
    changed = await storage.get_record(record.record_id)
    assert changed is not None and changed.tier == "cold"
    third = await storage.append_event(
        MemoryEvent(
            kind=EventKind.FORGET,
            namespace="a",
            payload={"record_id": record.record_id, "hard": True},
        )
    )
    assert third.seq is not None
    c = await storage.advance_records(third.seq, 1, fold_records)
    assert (c.cursor, c.applied_count, c.complete) == (third.seq, 1, True)
    assert await storage.get_record(record.record_id) is None
    assert await storage.get_offset("records") == third.seq
    old = await storage.advance_records(first.seq, 1, fold_records)
    assert (old.cursor, old.applied_count, old.complete) == (third.seq, 0, True)


@pytest.mark.asyncio
async def test_callback_failure_rolls_record_and_cursor_back(storage: SQLiteStorage) -> None:
    event = await storage.append_event(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="a",
            payload={"record": _record().model_dump(mode="json")},
        )
    )
    assert event.seq is not None

    async def fail(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
        await fold_records(view, events)
        raise RuntimeError("failed fold")

    with pytest.raises(RuntimeError, match="failed fold"):
        await storage.advance_records(event.seq, 1, fail)
    assert await storage.get_record("record-a") is None
    assert await storage.get_offset("records") == 0
    assert (await storage.advance_records(event.seq, 1, fold_records)).complete


@pytest.mark.asyncio
async def test_generic_records_offset_is_owned(storage: SQLiteStorage) -> None:
    with pytest.raises(StorageError, match="owns its ordered checkpoint"):
        await storage.set_offset("records", 10**9)
    assert await storage.get_offset("records") == 0


@pytest.mark.asyncio
async def test_malformed_record_id_shapes_cannot_commit(storage: SQLiteStorage) -> None:
    malformed = [
        MemoryEvent(kind=EventKind.FORGET, namespace="a"),
        MemoryEvent(kind=EventKind.DECAY_TRANSITION, namespace="a"),
        MemoryEvent(kind=EventKind.RETRIEVE, namespace="a", payload={"record_ids": "bad"}),
        MemoryEvent(kind=EventKind.SESSION, namespace="a", payload={"record_ids": [""]}),
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace="a",
            payload={"marker": "erasure_manifest", "hidden": "bad"},
        ),
    ]
    for event in malformed:
        with pytest.raises(StorageError):
            await storage.append_event(event)
    with pytest.raises(StorageError):
        await storage.append_with_outbox(malformed[0], [])
    assert await storage.read_events() == []
    await storage.append_event(
        MemoryEvent(
            kind=EventKind.FEEDBACK,
            namespace="a",
            payload={"feedback": {"channel": "audit"}},
        )
    )
    await storage.append_event(
        MemoryEvent(kind=EventKind.MARKER, namespace="a", payload={"marker": "unknown"})
    )
    assert len(await storage.read_events()) == 2


@pytest.mark.asyncio
async def test_append_fence_survives_reset_and_blocks_outbox_write(storage: SQLiteStorage) -> None:
    record = _record()
    write = MemoryEvent(
        kind=EventKind.WRITE, namespace="a", payload={"record": record.model_dump(mode="json")}
    )
    first = await storage.append_event(write)
    hard = await storage.append_event(
        MemoryEvent(
            kind=EventKind.FORGET,
            namespace="a",
            payload={"record_id": record.record_id, "hard": True},
        )
    )
    assert first.seq is not None and hard.seq is not None
    with pytest.raises(StorageError, match="permanently erased"):
        await storage.append_event(write)
    scope = OwnerScope(tenant="tenant", owner="owner")
    with pytest.raises(StorageError, match="permanently erased"):
        await storage.append_with_outbox(write, [OutboxItem(scope, "stage", {"id": "job"})])
    assert await storage.outbox_items(scope, "stage") == []
    assert len(await storage.read_events()) == 2
    await storage.reset_records_projection()
    assert await storage.get_offset("records") == 0
    with pytest.raises(StorageError, match="permanently erased"):
        await storage.append_event(write)
    assert (await storage.advance_records(hard.seq, 2, fold_records)).complete
    assert await storage.get_record(record.record_id) is None


@pytest.mark.asyncio
async def test_unprojected_append_identity_blocks_foreign_erasure(storage: SQLiteStorage) -> None:
    record = _record()
    write = MemoryEvent(
        kind=EventKind.WRITE, namespace="a", payload={"record": record.model_dump(mode="json")}
    )
    first = await storage.append_event(write)
    assert first.seq is not None
    with pytest.raises(StorageError, match="another namespace"):
        await storage.append_event(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace="b",
                payload={"record_id": record.record_id, "hard": True},
            )
        )
    with pytest.raises(StorageError, match="event namespace"):
        await storage.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="b",
                payload={"record": record.model_dump(mode="json")},
            )
        )
    second = await storage.append_event(
        MemoryEvent(kind=EventKind.WRITE, namespace="a", payload=write.payload)
    )
    assert second.seq is not None
    assert (await storage.advance_records(second.seq, 2, fold_records)).complete
    assert await storage.get_record(record.record_id) is not None
    await storage.append_event(
        MemoryEvent(
            kind=EventKind.FORGET,
            namespace="a",
            payload={"record_id": record.record_id, "hard": False},
        )
    )
    await storage.append_event(
        MemoryEvent(kind=EventKind.WRITE, namespace="a", payload=write.payload)
    )  # soft forget never installs a permanent fence


@pytest.mark.asyncio
async def test_unprojected_identity_blocks_foreign_mutations_and_direct_upsert(
    storage: SQLiteStorage,
) -> None:
    record = _record()
    await storage.append_event(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="a",
            payload={"record": record.model_dump(mode="json")},
        )
    )
    foreign_events = [
        MemoryEvent(kind=EventKind.FORGET, namespace="b", payload={"record_id": record.record_id}),
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace="b",
            payload={"record_id": record.record_id, "set": {"tier": "cold"}},
        ),
        MemoryEvent(
            kind=EventKind.RETRIEVE, namespace="b", payload={"record_ids": [record.record_id]}
        ),
        MemoryEvent(
            kind=EventKind.FEEDBACK, namespace="b", payload={"record_id": record.record_id}
        ),
        MemoryEvent(
            kind=EventKind.SESSION, namespace="b", payload={"record_ids": [record.record_id]}
        ),
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace="b",
            payload={"marker": "erasure_manifest", "hidden": [record.record_id]},
        ),
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace="b",
            payload={"marker": "erasure_release", "record_ids": [record.record_id]},
        ),
    ]
    for event in foreign_events:
        with pytest.raises(StorageError, match="another namespace"):
            await storage.append_event(event)
    with pytest.raises(StorageError, match="another namespace"):
        await storage.upsert_record(record.model_copy(update={"namespace": "b"}))
    assert len(await storage.read_events()) == 1
    assert await storage.get_record(record.record_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("erasure_kind", ["hard", "manifest"])
async def test_first_erasure_binds_never_written_id_to_namespace(
    storage: SQLiteStorage, erasure_kind: str
) -> None:
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

    await storage.append_event(erase("a"))
    await storage.append_event(erase("a"))  # same owner may repeat an erasure
    assert await storage.hidden_or_erased_record_ids("b", ["never-written"]) == {"never-written"}
    with pytest.raises(StorageError, match="another namespace"):
        await storage.append_event(erase("b"))
    assert len(await storage.read_events()) == 2
    await storage.reset_records_projection()
    with pytest.raises(StorageError, match="another namespace"):
        await storage.append_event(erase("b"))
    assert len(await storage.read_events()) == 2


@pytest.mark.asyncio
async def test_erasure_manifest_fences_global_id(storage: SQLiteStorage) -> None:
    record = _record()
    await storage.append_event(
        MemoryEvent(
            kind=EventKind.WRITE, namespace="a", payload={"record": record.model_dump(mode="json")}
        )
    )
    await storage.append_event(
        MemoryEvent(
            kind=EventKind.MARKER,
            namespace="a",
            payload={
                "marker": "erasure_manifest",
                "manifest": {"ids": {"record_ids": [record.record_id]}},
            },
        )
    )
    with pytest.raises(StorageError, match="permanently erased"):
        await storage.append_event(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace="a",
                payload={"record": record.model_dump(mode="json")},
            )
        )


@pytest.mark.asyncio
async def test_trusted_replay_reconstructs_fence_without_cursor_poison(
    storage: SQLiteStorage,
) -> None:
    record = _record()
    payload = {"record": record.model_dump(mode="json")}
    first = await storage._append_replayed_event(
        MemoryEvent(kind=EventKind.WRITE, namespace="a", payload=payload)
    )
    await storage._append_replayed_event(
        MemoryEvent(
            kind=EventKind.FORGET,
            namespace="a",
            payload={"record_id": record.record_id, "hard": True},
        )
    )
    later = await storage._append_replayed_event(
        MemoryEvent(kind=EventKind.WRITE, namespace="a", payload=payload)
    )
    assert first.seq is not None and later.seq is not None
    receipt = await storage.advance_records(later.seq, 3, fold_records)
    assert (receipt.cursor, receipt.applied_count, receipt.complete) == (later.seq, 3, True)
    assert await storage.get_record(record.record_id) is None
    with pytest.raises(StorageError, match="permanently erased"):
        await storage.append_event(
            MemoryEvent(kind=EventKind.WRITE, namespace="a", payload=payload)
        )


@pytest.mark.asyncio
async def test_indexed_group_pages_and_id_only_membership(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = ClaimGroup("a", "tenant", "owner", "person", (), "subject", "born_in", "digest", 1, ())
    key = group_key(group)
    monkeypatch.setattr(sql_base, "record_group_of", lambda record: group)
    marker = await storage.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a"))
    assert marker.seq is not None

    async def put(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
        assert [event.seq for event in events] == [marker.seq]
        first = _record()
        second = first.model_copy(update={"record_id": "record-b"})
        await view.put_record(second)
        await view.put_record(first)
        assert [
            record.record_id for record in await view.group_page("a", key, after_id=None, limit=1)
        ] == ["record-a"]
        assert [
            record.record_id
            for record in await view.group_page("a", key, after_id="record-a", limit=1)
        ] == ["record-b"]
        await view.mark_erased("a", "record-a")
        await view.mark_hidden("a", "record-a")
        assert await view.erasure_state("a", "record-a") == (True, True)
        await view.release_hidden("a", "record-a")
        assert await view.erasure_state("a", "record-a") == (True, False)

    assert (await storage.advance_records(marker.seq, 1, put)).complete
    assert [
        record.record_id
        for record in await storage.claim_group_page("a", key, after_id="record-a", limit=1)
    ] == ["record-b"]
    assert [record.record_id for record in await storage.claim_group_snapshot("a", key)] == [
        "record-b"
    ]
    assert await storage.hidden_or_erased_record_ids("a", ["record-a", "record-b"]) == {"record-a"}
    next_marker = await storage.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a"))
    assert next_marker.seq is not None

    async def check(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
        assert await view.erasure_state("a", "record-a") == (True, False)
        await view.delete_record("record-a")
        assert [
            record.record_id for record in await view.group_page("a", key, after_id=None, limit=2)
        ] == ["record-b"]

    await storage.advance_records(next_marker.seq, 1, check)
    assert await storage.get_record("record-a") is None
    assert [
        record.record_id
        for record in await storage.claim_group_page("a", key, after_id=None, limit=2)
    ] == ["record-b"]
    assert [record.record_id for record in await storage.claim_group_snapshot("a", key)] == [
        "record-b"
    ]


@pytest.mark.asyncio
async def test_complete_indexed_group_snapshot_exceeds_page_size(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = ClaimGroup("a", "tenant", "owner", "person", (), "subject", "born_in", "digest", 1, ())
    key = group_key(group)
    monkeypatch.setattr(sql_base, "record_group_of", lambda record: group)
    marker = await storage.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a"))
    assert marker.seq is not None

    async def populate(view: RecordsProjectionView, events: Sequence[MemoryEvent]) -> None:
        for number in range(257):
            await view.put_record(_record().model_copy(update={"record_id": f"record-{number:03}"}))

    await storage.advance_records(marker.seq, 1, populate)
    peers = await storage.claim_group_snapshot("a", key)
    assert len(peers) == 257
    assert [peer.record_id for peer in peers] == [f"record-{number:03}" for number in range(257)]


@pytest.mark.asyncio
async def test_active_untyped_fact_uses_indexed_selector_amid_typed_fact(
    storage: SQLiteStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = ClaimGroup("a", "tenant", "owner", "person", (), "subject", "city", "digest", 1, ())
    monkeypatch.setattr(
        sql_base, "record_group_of", lambda record: group if record.record_id == "typed" else None
    )
    now = datetime.now(UTC)
    base = _record().model_copy(update={"entity": "person", "attribute": "city"})
    berlin = base.model_copy(
        update={"record_id": "berlin", "content": "Berlin", "recorded_at": now - timedelta(3)}
    )
    lisbon = base.model_copy(
        update={"record_id": "typed", "content": "Lisbon", "recorded_at": now - timedelta(2)}
    )
    porto = base.model_copy(
        update={"record_id": "porto", "content": "Porto", "recorded_at": now - timedelta(1)}
    )
    await storage.upsert_record(berlin)
    await storage.upsert_record(lisbon)
    latest = await storage.find_active_fact("a", "person", "city")
    untyped = await storage.find_active_untyped_fact("a", "person", "city")
    assert latest is not None and latest.record_id == "typed"
    assert untyped is not None and untyped.record_id == "berlin"
    await storage.upsert_record(porto)
    untyped = await storage.find_active_untyped_fact("a", "person", "city")
    assert untyped is not None and untyped.record_id == "porto"
    assert (await storage.find_active_untyped_fact("b", "person", "city")) is None


@pytest.mark.asyncio
async def test_peer_snapshot_honors_native_hidden_and_release(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    group = ClaimGroup("a", "tenant", "owner", "person", (), "subject", "born_in", "digest", 1, ())
    key = group_key(group)
    monkeypatch.setattr(sql_base, "record_group_of", lambda record: group)
    path = str(tmp_path / "peer-visibility.db")
    clients = [SQLiteClient(SQLiteSpec(path, busy_timeout_s=10.0)) for _ in range(2)]
    stores = [SQLiteStorage(client) for client in clients]
    try:
        for client in clients:
            await client.open()
        await stores[0].start()
        await stores[1].start()
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
        ids = [f"unrelated-{number}" for number in range(1100)] + ["record-a", "record-b"]
        assert await stores[1].hidden_or_erased_record_ids("a", ids) == {"record-b"}
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


@pytest.mark.asyncio
async def test_ephemeral_append_folds_atomically(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "ephemeral.db")))
    await client.open()
    store = SQLiteStorage(client, mode=EventLogMode.EPHEMERAL)
    try:
        await store.start()
        record = _record()
        write = MemoryEvent(
            kind=EventKind.WRITE, namespace="a", payload={"record": record.model_dump(mode="json")}
        )
        with pytest.raises(StorageError, match="append_ephemeral_records"):
            await store.append_event(write)
        assert await store.read_events() == []
        with pytest.raises(StorageError, match="record_id"):
            await store.append_ephemeral_records(
                MemoryEvent(kind=EventKind.FORGET, namespace="a"), fold_records
            )

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
        # The failed hard-FORGET transaction did not leave a permanent fence.
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
        harmless = await store.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a"))
        assert harmless.seq == 4
    finally:
        await store.stop()
        await client.close()


@pytest.mark.asyncio
async def test_records_registration_and_reset_protect_rolling_history(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "rolling.db")))
    await client.open()
    store = SQLiteStorage(client, mode=EventLogMode.ROLLING)
    try:
        await store.start()
        old = datetime.now(UTC) - timedelta(days=2)
        first = await store.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a", ts=old))
        assert first.seq is not None
        await store.set_offset("other_projector", first.seq)
        await store.initialize_records_projection()
        assert await store.prune_events(datetime.now(UTC)) == 0
        await store.advance_records(first.seq, 1, fold_records)
        await store.reset_records_projection()
        assert await store.get_offset("records") == 0
        assert await store.prune_events(datetime.now(UTC)) == 0
    finally:
        await store.stop()
        await client.close()


@pytest.mark.asyncio
async def test_late_records_enable_refuses_fully_pruned_history(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "late.db")))
    await client.open()
    store = SQLiteStorage(client, mode=EventLogMode.ROLLING)
    try:
        await store.start()
        old = datetime.now(UTC) - timedelta(days=2)
        event = await store.append_event(MemoryEvent(kind=EventKind.MARKER, namespace="a", ts=old))
        assert event.seq is not None
        await store.set_offset("other_projector", event.seq)
        assert await store.prune_events(datetime.now(UTC)) == 1
        assert await store.read_events() == []
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.initialize_records_projection()
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.advance_records(event.seq, 1, fold_records)
        with pytest.raises(RebuildUnavailableError, match="history was pruned"):
            await store.reset_records_projection()
        assert await store.get_offset("records") == 0
    finally:
        await store.stop()
        await client.close()
