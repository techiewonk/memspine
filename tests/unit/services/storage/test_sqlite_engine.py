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


async def test_ephemeral_offsets_never_touch_the_database_file(tmp_path: Path) -> None:
    """Regression: ephemeral runs used to persist offsets, poisoning later
    full-mode runs on the same file (stale high-water mark > real max seq)."""
    db = tmp_path / "spine.db"

    client1 = SQLiteClient(db)
    await client1.connect()
    ephemeral = SQLiteStorage(client1, mode=EventLogMode.EPHEMERAL)
    await ephemeral.start()
    for i in range(5):
        appended = await ephemeral.append_event(ev(content=str(i)))
        await ephemeral.set_offset("records", appended.seq or 0)
    assert await ephemeral.get_offset("records") == 5  # in-memory only
    await client1.close()

    client2 = SQLiteClient(db)
    await client2.connect()
    full = SQLiteStorage(client2, mode=EventLogMode.FULL)
    await full.start()
    assert await full.get_offset("records") == 0  # nothing leaked to disk
    projector = DictProjector("records")
    e1 = await full.append_event(ev(content="first real event"))
    assert e1.seq == 1
    applied = await catch_up(full, [projector])
    assert applied["records"] == 1  # the first full-mode event is projected
    await client2.close()


async def test_migration_0006_backfills_existing_rows_in_place(tmp_path: Path) -> None:
    """Upgrade path: a populated 0005 database gains NULL skill_stage (never
    entered the ladder) and reflection_depth=0 (raw) on its existing rows."""
    from alembic import command
    from sqlalchemy import create_engine, text

    from memspine.services.storage.sqlite.migrations import alembic_config

    db = tmp_path / "old.db"
    command.upgrade(alembic_config(db), "0005")
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_records (record_id, namespace, memory_type, content,"
                " content_fingerprint, valid_from, recorded_at, source, status, version,"
                " history, pii_tier, consent_tags, scoring, trust, quarantined,"
                " instruction_flag, corroborations) VALUES ('r1', 'agent/a', 'semantic',"
                " 'kept', 'fp', '2026-01-01', '2026-01-01', X'7B7D', 'activated', 1,"
                " X'5B5D', 'none', X'5B5D', X'7B7D', 0.5, 0, 0, 0)"
            )
        )
    command.upgrade(alembic_config(db), "head")
    with engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT content, skill_stage, reflection_depth"
                " FROM memory_records WHERE record_id='r1'"
            )
        ).one()
    engine.dispose()
    assert row[0] == "kept" and row[1] is None and row[2] == 0


async def test_migration_0004_backfills_existing_rows_in_place(tmp_path: Path) -> None:
    """Upgrade path: a populated 0003 database gains tier='hot' and NULL
    content_zstd on its existing rows without loss (server_default backfill)."""
    from alembic import command
    from sqlalchemy import create_engine, text

    from memspine.services.storage.sqlite.migrations import alembic_config

    db = tmp_path / "old.db"
    command.upgrade(alembic_config(db), "0003")
    engine = create_engine(f"sqlite:///{db}")
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO memory_records (record_id, namespace, memory_type, content,"
                " content_fingerprint, valid_from, recorded_at, source, status, version,"
                " history, pii_tier, consent_tags, scoring, trust, quarantined,"
                " instruction_flag) VALUES ('r1', 'agent/a', 'semantic', 'kept',"
                " 'fp', '2026-01-01', '2026-01-01', X'7B7D', 'activated', 1, X'5B5D',"
                " 'none', X'5B5D', X'7B7D', 0.5, 0, 0)"
            )
        )
    command.upgrade(alembic_config(db), "head")
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT content, tier, content_zstd FROM memory_records WHERE record_id='r1'")
        ).one()
    engine.dispose()
    assert row[0] == "kept" and row[1] == "hot" and row[2] is None


async def test_migration_0007_creates_working_graph_tables(tmp_path: Path) -> None:
    """Upgrade path: a populated 0006 database gains the P6 graph tables, and
    the SQLite adjacency adapter round-trips against them (functional proof,
    not just DDL presence)."""
    from alembic import command

    from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
    from memspine.services.storage.sqlite.migrations import alembic_config

    db = tmp_path / "old.db"
    command.upgrade(alembic_config(db), "0006")
    command.upgrade(alembic_config(db), "head")

    client = SQLiteClient(db)
    await client.connect()
    try:
        graph = SQLiteAdjacencyGraph(client)
        await graph.upsert_node("m1", labels=["memory"], properties={"kind": "fact"})
        await graph.upsert_edge("m1", "m2", "related", {"weight": 0.8})
        assert await graph.node_count() == 2  # m2 implicitly created
        assert {n.node_id for n in await graph.neighbors("m1")} == {"m2"}
        [edge] = await graph.edges_of("m2")
        assert (edge.src, edge.dst, edge.rel_type, edge.weight) == ("m1", "m2", "related", 0.8)
    finally:
        await client.close()


async def test_migration_0008_creates_lexical_fts_index(tmp_path: Path) -> None:
    """Upgrade path: a populated 0007 database gains the D-25 lexical index,
    and the FTS5 store indexes + searches against the migrated table."""
    from alembic import command
    from sqlalchemy import create_engine, inspect

    from memspine.core.records import SourceInfo
    from memspine.services.lexical.sqlite_fts5 import SQLiteFTS5Lexical
    from memspine.services.storage.sqlite.migrations import alembic_config

    db = tmp_path / "old.db"
    command.upgrade(alembic_config(db), "0007")
    command.upgrade(alembic_config(db), "head")

    engine = create_engine(f"sqlite:///{db}")
    assert inspect(engine).has_table("memory_fts")  # table exists after upgrade
    engine.dispose()

    client = SQLiteClient(db)
    await client.connect()
    try:
        store = SQLiteFTS5Lexical(client)
        await store.index(
            MemoryRecord(
                record_id="r1",
                namespace="agent/a",
                memory_type="semantic",
                content="the migrated fox",
                source=SourceInfo(role="user"),
            )
        )
        hits = await store.search("agent/a", "fox")
        assert [h.record_id for h in hits] == ["r1"]
    finally:
        await client.close()


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
