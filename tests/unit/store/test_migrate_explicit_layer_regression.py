"""An ordinary rebuild preserves an explicit historical layer; migration fixes it."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from memspine.clients.sqlite import SQLiteClient
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.migrate import migrate_sqlite
from memspine.store.records import MemoryRecord, SourceInfo
from memspine.store.replay import rebuild


async def test_explicit_wrong_producer_layer_needs_migrate_sqlite(tmp_path: Path) -> None:
    source_path = tmp_path / "historical.db"
    target_path = tmp_path / "migrated.db"
    client = SQLiteClient(str(source_path))
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    try:
        created = datetime(2026, 1, 1, tzinfo=UTC)
        record = MemoryRecord(
            record_id="legacy-summary",
            namespace="migration-test",
            memory_type="semantic",
            content="A synthetic historical summary",
            source=SourceInfo(role="assistant", channel="consolidation"),
            layer="L0",
            valid_from=created,
            recorded_at=created,
        )
        await storage.append_event(
            MemoryEvent(
                event_id="legacy-write",
                kind=EventKind.WRITE,
                namespace=record.namespace,
                ts=created,
                actor="regression-test",
                schema_version=2,
                payload={"record": record.model_dump(mode="json")},
            )
        )

        # This is the ordinary projection rebuild used by Engine.rebuild(): replay the
        # current event as stored. It does not run the migration-only v2 -> v3 upcaster.
        await rebuild(storage, RecordProjector(storage))
        ordinary = await storage.get_record(record.record_id)
        assert ordinary is not None and ordinary.layer == "L0"
    finally:
        await client.close()

    source_before = source_path.read_bytes()
    report = await migrate_sqlite(str(source_path), str(target_path))
    assert report.events_upcast == 1

    # Confirm the input's explicit claim and schema version survived untouched.
    verify_client = SQLiteClient(str(source_path))
    await verify_client.connect()
    verify_storage = SQLiteStorage(verify_client)
    await verify_storage.start()
    try:
        source_record = await verify_storage.get_record("legacy-summary")
        source_events = await verify_storage.read_events()
        assert source_record is not None and source_record.layer == "L0"
        assert len(source_events) == 1 and source_events[0].schema_version == 2
        assert source_events[0].payload["record"]["source"]["channel"] == "consolidation"
    finally:
        await verify_client.close()
    assert source_path.read_bytes() == source_before

    target_client = SQLiteClient(str(target_path))
    await target_client.connect()
    target_storage = SQLiteStorage(target_client)
    await target_storage.start()
    try:
        migrated = await target_storage.get_record("legacy-summary")
        target_events = await target_storage.read_events()
        assert migrated is not None and migrated.layer == "L2"
        assert len(target_events) == 1 and target_events[0].schema_version == 3
        assert target_events[0].payload["record"]["layer"] == "L2"
    finally:
        await target_client.close()
