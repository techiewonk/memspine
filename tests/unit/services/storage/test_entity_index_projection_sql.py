"""The SQL entity projector's transaction and conservative prefix contract."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import delete, select

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.exceptions import RebuildUnavailableError, StorageError
from memspine.services.storage.entity_index_tables import EntityIndexRow
from memspine.services.storage.records_port import (
    EntityIndexEffects,
    EntityIndexProjectionView,
)
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.storage.sqlite.schema import memory_events, projector_offsets
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.records import MemoryRecord


@pytest.fixture(params=["memory", "file"])
async def storage(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[SQLiteStorage]:
    path = ":memory:" if request.param == "memory" else str(tmp_path / "projection.db")
    client = SQLiteClient(SQLiteSpec(path, busy_timeout_s=10.0))
    await client.open()
    store = SQLiteStorage(client)
    try:
        await store.start()
        await store.assert_full_event_history()
        async with client.engine.connect() as conn:
            before = (
                await conn.execute(
                    select(projector_offsets.c.last_seq).where(
                        projector_offsets.c.projector_name == "entity_index"
                    )
                )
            ).all()
        assert before == []
        await store.initialize_entity_index()
        yield store
    finally:
        await store.stop()
        await client.close()


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


async def _event(storage: SQLiteStorage, namespace: str, kind: EventKind = EventKind.WRITE) -> int:
    payload = (
        {"record_id": uuid.uuid4().hex}
        if kind in {EventKind.FORGET, EventKind.DECAY_TRANSITION}
        else {}
    )
    event = await storage.append_event(
        _write(namespace)
        if kind is EventKind.WRITE
        else MemoryEvent(kind=kind, namespace=namespace, payload=payload)
    )
    assert event.seq is not None
    return event.seq


async def _apply(
    view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
) -> EntityIndexEffects:
    for event in events:
        assert event.seq is not None
        assert await view.get_record(f"missing-{event.seq}") is None
        row = EntityIndexRow(
            event.namespace, "entity", str(event.seq), str(event.seq), seq=event.seq
        )
        await view.put([row])
        assert await view.select(event.namespace, "entity", key=str(event.seq)) == [row]
    return EntityIndexEffects(changed=bool(events))


@pytest.mark.asyncio
async def test_bound_target_page_and_revision(storage: SQLiteStorage) -> None:
    first = await _event(storage, "a")
    second = await _event(storage, "a", EventKind.DECAY_TRANSITION)
    third = await _event(storage, "a", EventKind.FORGET)
    fourth = await _event(storage, "a", EventKind.MARKER)
    receipt = await storage.advance_entity_index("a", third, 1, _apply)
    assert (receipt.namespace_cursor, receipt.complete, receipt.applied_count) == (first, False, 1)
    assert receipt.namespace_mutation_revision == 1
    receipt = await storage.advance_entity_index("a", third, 2, _apply)
    assert (receipt.namespace_cursor, receipt.complete, receipt.applied_count) == (third, True, 2)
    assert receipt.namespace_mutation_revision == 2
    assert await storage.entity_index_store().select("a", "entity", key=str(fourth)) == []
    assert second < third < fourth
    old = await storage.advance_entity_index("a", first, 1, _apply)
    assert (old.namespace_cursor, old.complete, old.applied_count) == (third, True, 0)


@pytest.mark.asyncio
async def test_callback_failure_and_cancel_roll_back(storage: SQLiteStorage) -> None:
    seq = await _event(storage, "a")

    async def fail(
        view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
    ) -> EntityIndexEffects:
        await _apply(view, events)
        raise RuntimeError("callback failed")

    with pytest.raises(RuntimeError, match="callback failed"):
        await storage.advance_entity_index("a", seq, 1, fail)
    assert await storage.entity_index_store().select("a", "entity") == []
    assert await storage.entity_index_revision("a") == (0, 0)

    async def cancel(
        view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
    ) -> EntityIndexEffects:
        await _apply(view, events)
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await storage.advance_entity_index("a", seq, 1, cancel)
    assert await storage.entity_index_store().select("a", "entity") == []
    assert (await storage.advance_entity_index("a", seq, 1, _apply)).namespace_cursor == seq


@pytest.mark.asyncio
async def test_foreign_prefix_reset_and_purge(storage: SQLiteStorage) -> None:
    foreign = await _event(storage, "b")
    own = await _event(storage, "a")
    receipt = await storage.advance_entity_index("a", own, 2, _apply)
    assert receipt.namespace_cursor == own
    assert receipt.global_offset < foreign

    async def purge(view: EntityIndexProjectionView, ids: Sequence[str]) -> EntityIndexEffects:
        assert ids == ["source"]
        await view.delete("a", "entity", key=str(own))
        return EntityIndexEffects(changed=True, removed_entity_ids=(str(own),))

    purged = await storage.purge_entity_index("a", ["source"], purge)
    assert purged.namespace_cursor == own
    assert purged.namespace_mutation_revision == 2
    assert await storage.entity_index_store().select("a", "entity") == []
    reset = await storage.reset_entity_index()
    assert (reset.epoch, reset.global_offset) == (1, 0)
    assert await storage.get_offset("entity_index") == 0
    assert await storage.entity_index_revision("a") == (1, 0)
    receipt = await storage.advance_entity_index("b", foreign, 2, _apply)
    assert receipt.global_offset >= foreign


@pytest.mark.asyncio
async def test_pending_marker_retries_without_rescanning_new_events(storage: SQLiteStorage) -> None:
    marker = await _event(storage, "a", EventKind.MARKER)
    later = await _event(storage, "a", EventKind.WRITE)
    seen: list[list[int]] = []

    async def defer(
        view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
    ) -> EntityIndexEffects:
        seen.append([int(event.seq or 0) for event in events])
        pending = await view.pending_events(1)
        if pending:
            assert [event.seq for event in pending] == [marker]
            assert pending[0].kind is EventKind.MARKER
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

    first = await storage.advance_entity_index("a", marker, 1, defer)
    assert (first.namespace_cursor, first.complete, first.global_offset) == (0, False, 0)
    second = await storage.advance_entity_index("a", later, 1, defer)
    assert (second.namespace_cursor, second.complete, second.applied_count) == (0, False, 1)
    third = await storage.advance_entity_index("a", later, 1, defer)
    assert (third.namespace_cursor, third.complete, third.applied_count) == (later, True, 0)
    assert seen == [[marker], [later], []]


@pytest.mark.asyncio
@pytest.mark.parametrize("foreign_first", [False, True])
async def test_purge_recertifies_global_prefix_with_foreign_barrier(
    storage: SQLiteStorage, foreign_first: bool
) -> None:
    if foreign_first:
        await _event(storage, "b")
    marker = await _event(storage, "a", EventKind.MARKER)

    async def make_pending(
        view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
    ) -> EntityIndexEffects:
        assert [event.seq for event in events] == [marker]
        await view.put(
            [EntityIndexRow("a", "pending_positive", str(marker), "opaque-id", "alias", seq=marker)]
        )
        return EntityIndexEffects(changed=True)

    blocked = await storage.advance_entity_index("a", marker, 1, make_pending)
    assert blocked.namespace_cursor == marker - 1
    assert blocked.global_offset == 0

    async def purge_pending(
        view: EntityIndexProjectionView, ids: Sequence[str]
    ) -> EntityIndexEffects:
        assert ids == ["evidence"]
        await view.delete("a", "pending_positive", key=str(marker))
        return EntityIndexEffects(changed=True)

    released = await storage.purge_entity_index("a", ["evidence"], purge_pending)
    expected_global = 0 if foreign_first else marker
    assert (released.namespace_cursor, released.global_offset) == (marker, expected_global)
    assert await storage.get_offset("entity_index") == expected_global


@pytest.mark.asyncio
async def test_missing_pending_canonical_marker_fails_closed(storage: SQLiteStorage) -> None:
    marker = await _event(storage, "a", EventKind.MARKER)

    async def mark_pending(
        view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
    ) -> EntityIndexEffects:
        assert events[0].seq == marker
        await view.put(
            [EntityIndexRow("a", "pending_positive", str(marker), "opaque-id", "alias", seq=marker)]
        )
        return EntityIndexEffects(changed=True)

    first = await storage.advance_entity_index("a", marker, 1, mark_pending)
    assert first.namespace_cursor == 0
    async with storage._client.engine.begin() as conn:
        await conn.execute(delete(memory_events).where(memory_events.c.seq == marker))

    async def retry(
        view: EntityIndexProjectionView, events: Sequence[MemoryEvent]
    ) -> EntityIndexEffects:
        assert events == []
        await view.pending_events(1)
        return EntityIndexEffects()

    with pytest.raises(StorageError, match="canonical event is missing"):
        await storage.advance_entity_index("a", marker, 1, retry)
    assert await storage.entity_index_store().select("a", "pending_positive")
    assert await storage.entity_index_revision("a") == (0, 1)


@pytest.mark.asyncio
async def test_generic_entity_offset_cannot_bypass_retention(storage: SQLiteStorage) -> None:
    seq = await _event(storage, "a")
    with pytest.raises(StorageError, match="owns its ordered checkpoint"):
        await storage.set_offset("entity_index", seq + 1000)
    assert await storage.get_offset("entity_index") == 0
    receipt = await storage.advance_entity_index("a", seq, 1, _apply)
    assert receipt.namespace_cursor == seq


@pytest.mark.asyncio
async def test_zero_checkpoint_survives_reset_and_blocks_rolling_prune(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "rolling.db")))
    await client.open()
    store = SQLiteStorage(client, mode=EventLogMode.ROLLING)
    try:
        await store.start()
        await store.initialize_entity_index()
        old = datetime.now(UTC) - timedelta(days=2)
        event = await store.append_event(_write("a", ts=old))
        assert event.seq is not None
        await store.set_offset("other_projector", event.seq)
        assert await store.prune_events(datetime.now(UTC)) == 0
        await store.advance_entity_index("a", event.seq, 1, _apply)
        await store.reset_entity_index()
        assert await store.prune_events(datetime.now(UTC)) == 0
        assert len(await store.read_events()) == 1
        async with client.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(projector_offsets.c.last_seq).where(
                        projector_offsets.c.projector_name == "entity_index"
                    )
                )
            ).all()
        assert [row.last_seq for row in rows] == [0]
    finally:
        await store.stop()
        await client.close()


@pytest.mark.asyncio
async def test_first_enable_rejects_fully_pruned_history(tmp_path: Path) -> None:
    client = SQLiteClient(SQLiteSpec(str(tmp_path / "late-enable.db")))
    await client.open()
    store = SQLiteStorage(client, mode=EventLogMode.ROLLING)
    try:
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
        assert await store.get_offset("entity_index") == 0
        async with client.engine.connect() as conn:
            rows = (
                await conn.execute(
                    select(projector_offsets.c.last_seq).where(
                        projector_offsets.c.projector_name == "entity_index"
                    )
                )
            ).all()
        assert rows == []
    finally:
        await store.stop()
        await client.close()
