"""Walking-skeleton proof: single write door, D-45 modes, replay + rebuild."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, EventLogMode, MemoryEvent
from memspine.core.projector import Projector
from memspine.core.records import MemoryRecord, PiiTier, RecordStatus, SourceInfo
from memspine.core.replay import catch_up, rebuild
from memspine.exceptions import RebuildUnavailableError, StorageError
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.storage.sqlite.migrations import upgrade_to_head


class DictProjector(Projector):
    """Minimal idempotent projection: event_id -> namespace."""

    def __init__(self, name: str = "dict") -> None:
        self.name = name
        self.state: dict[str, str] = {}

    async def apply(self, event: MemoryEvent) -> None:
        self.state[event.event_id] = event.namespace

    async def reset(self) -> None:
        self.state.clear()


async def make_storage(
    mode: EventLogMode = EventLogMode.FULL, compress: bool = False
) -> SQLiteStorage:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client, mode=mode, compress=compress)
    await storage.start()
    return storage


def ev(namespace: str = "ns/a", **payload: object) -> MemoryEvent:
    return MemoryEvent(kind=EventKind.WRITE, namespace=namespace, payload=dict(payload))


async def test_append_assigns_monotonic_seq_and_round_trips() -> None:
    storage = await make_storage()
    e1 = await storage.append_event(ev(content="one"))
    e2 = await storage.append_event(ev(content="two"))
    assert (e1.seq, e2.seq) == (1, 2)

    events = await storage.read_events()
    assert [e.payload["content"] for e in events] == ["one", "two"]
    assert events[0].fingerprint == e1.fingerprint
    assert events[0].ts.tzinfo is not None


async def test_write_door_rejects_resubmitted_events() -> None:
    storage = await make_storage()
    e1 = await storage.append_event(ev())
    with pytest.raises(StorageError):
        await storage.append_event(e1)


async def test_compressed_payloads_round_trip() -> None:
    storage = await make_storage(compress=True)
    big = "memory " * 500
    await storage.append_event(ev(content=big))
    events = await storage.read_events()
    assert events[0].payload["content"] == big


async def test_catch_up_applies_only_new_events_and_checkpoints() -> None:
    storage = await make_storage()
    projector = DictProjector()
    for i in range(3):
        await storage.append_event(ev(content=str(i)))

    applied = await catch_up(storage, [projector])
    assert applied["dict"] == 3
    assert await storage.get_offset("dict") == 3

    # second catch-up is a no-op; new event applies incrementally
    assert (await catch_up(storage, [projector]))["dict"] == 0
    await storage.append_event(ev(content="3"))
    assert (await catch_up(storage, [projector]))["dict"] == 1
    assert len(projector.state) == 4


async def test_rebuild_replays_from_zero_to_identical_state() -> None:
    storage = await make_storage()
    projector = DictProjector()
    for i in range(5):
        await storage.append_event(ev(content=str(i)))
    await catch_up(storage, [projector])
    before = dict(projector.state)

    count = await rebuild(storage, projector)
    assert count == 5
    assert projector.state == before


async def test_ephemeral_mode_never_persists_but_still_sequences() -> None:
    storage = await make_storage(mode=EventLogMode.EPHEMERAL)
    e1 = await storage.append_event(ev(content="gone"))
    e2 = await storage.append_event(ev(content="gone too"))
    assert (e1.seq, e2.seq) == (1, 2)
    assert await storage.read_events() == []
    assert storage.can_rebuild is False
    with pytest.raises(RebuildUnavailableError):
        await rebuild(storage, DictProjector())


async def test_rolling_prune_never_passes_slowest_projector() -> None:
    storage = await make_storage(mode=EventLogMode.ROLLING)
    fast, slow = DictProjector("fast"), DictProjector("slow")
    for i in range(4):
        await storage.append_event(ev(content=str(i)))

    await catch_up(storage, [fast])  # fast at seq 4
    await storage.set_offset("slow", 2)  # slow stuck at seq 2

    cutoff = datetime.now(UTC) + timedelta(days=1)  # everything is "old enough"
    pruned = await storage.prune_events(older_than=cutoff)
    assert pruned == 2  # only seq 1..2: never past slow's high-water mark

    remaining = await storage.read_events()
    assert [e.seq for e in remaining] == [3, 4]
    # slow can still finish catching up
    await catch_up(storage, [slow])
    assert len(slow.state) == 2


async def test_prune_is_noop_in_full_mode() -> None:
    storage = await make_storage(mode=EventLogMode.FULL)
    await storage.append_event(ev())
    await catch_up(storage, [DictProjector()])
    assert await storage.prune_events(datetime.now(UTC) + timedelta(days=1)) == 0
    assert len(await storage.read_events()) == 1


async def test_record_upsert_get_round_trip_preserves_all_fields() -> None:
    storage = await make_storage()
    record = MemoryRecord(
        namespace="agent/alice",
        memory_type="semantic",
        content="the sky is blue",
        source=SourceInfo(role="user", channel="chat", message_id="m-1"),
        pii_tier=PiiTier.LOW,
        consent_tags=["chat"],
        trust=0.8,
        simhash=12345,
        minhash_sig=b"\x01\x02",
    )
    await storage.upsert_record(record)
    loaded = await storage.get_record(record.record_id)
    assert loaded is not None
    assert loaded.model_dump() == record.model_dump()

    # update path
    updated = record.model_copy(update={"status": RecordStatus.ARCHIVED, "version": 2})
    await storage.upsert_record(updated)
    loaded2 = await storage.get_record(record.record_id)
    assert loaded2 is not None and loaded2.version == 2
    assert loaded2.status is RecordStatus.ARCHIVED
    assert len(await storage.list_records("agent/alice")) == 1


async def test_alembic_migration_builds_same_schema(tmp_path: Path) -> None:
    db = tmp_path / "memspine.db"
    upgrade_to_head(db)

    client = SQLiteClient(db)
    await client.connect()
    storage = SQLiteStorage(client)  # no start(): schema must already exist
    storage._started = True  # bypass start() to prove the Alembic DDL alone suffices
    e1 = await storage.append_event(ev(content="via alembic"))
    assert e1.seq == 1
    await client.close()
