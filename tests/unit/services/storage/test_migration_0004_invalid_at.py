"""#19: migration 0004 adds the nullable ``invalid_at`` column to memory_records;
a pre-#19 row reads back with ``invalid_at=None``."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine, inspect

from memspine.clients.sqlite import SQLiteClient
from memspine.core.records import MemoryRecord
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.services.storage.sqlite.migrations import alembic_config, upgrade_to_head


def _columns(db: Path) -> set[str]:
    engine = create_engine(f"sqlite:///{db}")
    try:
        return {c["name"] for c in inspect(engine).get_columns("memory_records")}
    finally:
        engine.dispose()


async def test_fresh_head_has_invalid_at(tmp_path: Path) -> None:
    db = tmp_path / "fresh.db"
    upgrade_to_head(db)
    assert "invalid_at" in _columns(db)


async def test_downgrade_then_upgrade_round_trips_records(tmp_path: Path) -> None:
    db = tmp_path / "legacy.db"
    upgrade_to_head(db)
    cfg = alembic_config(db)
    command.downgrade(cfg, "0003")
    assert "invalid_at" not in _columns(db)
    command.upgrade(cfg, "head")
    assert "invalid_at" in _columns(db)

    client = SQLiteClient(str(db))
    await client.connect()
    try:
        storage = SQLiteStorage(client)
        plain = MemoryRecord(namespace="ns", memory_type="semantic", content="a")
        ended = MemoryRecord(
            namespace="ns",
            memory_type="semantic",
            content="b",
            invalid_at=datetime(2024, 5, 1, tzinfo=UTC),
        )
        await storage.upsert_record(plain)
        await storage.upsert_record(ended)
        got_plain = await storage.get_record(plain.record_id)
        got_ended = await storage.get_record(ended.record_id)
    finally:
        await client.close()
    assert got_plain is not None and got_plain.invalid_at is None
    assert got_ended is not None and got_ended.invalid_at == datetime(2024, 5, 1, tzinfo=UTC)


def test_old_payload_without_invalid_at_loads_and_dumps_unchanged() -> None:
    payload = MemoryRecord(namespace="ns", memory_type="semantic", content="x").model_dump(
        mode="json"
    )
    assert "invalid_at" not in payload  # None is omitted: logs stay byte-identical
    assert MemoryRecord.model_validate(payload).model_dump(mode="json") == payload
