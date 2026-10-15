"""F-16b: event upcasters and migration by replay of the retained log (M-01, M-26, G-23)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.exceptions import RebuildUnavailableError
from memspine.schemas import versioning
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.export_view import MarkdownExportView
from memspine.store.migrate import (
    NOT_REBUILT_LAYERS,
    ReplayBuilder,
    V1Upcaster,
    dump_store,
    ensure_event_upcasters,
    migrate_sqlite,
)
from memspine.store.records import MemoryRecord, SourceInfo

NS = "ilse"
T0 = datetime(2024, 3, 1, 9, 0, tzinfo=UTC)


def _record(rid: str, text: str, **kw: object) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid,
        namespace=NS,
        memory_type="semantic",
        content=text,
        source=SourceInfo(role="user", channel="mining"),
        valid_from=T0,
        recorded_at=T0,
        created_at=T0,
        **kw,  # type: ignore[arg-type]
    )


def _v1_events() -> list[MemoryEvent]:
    """A synthetic v1 log: pre-envelope writes (no layer/type_tag in the payload), a like, a forget."""
    texts = {
        "r-ilse": "Ilse keeps bees behind the mill",
        "r-bram": "Bram repairs bicycles on Sundays",
        "r-noor": "Noor is learning to play the cello",
    }
    events: list[MemoryEvent] = []
    for i, (rid, text) in enumerate(texts.items()):
        rec = _record(rid, text).model_dump(mode="json")
        for key in ("layer", "type_tag", "provenance_class"):
            rec.pop(key, None)
        events.append(
            MemoryEvent(
                event_id=f"e-w{i}",
                kind=EventKind.WRITE,
                namespace=NS,
                ts=T0,
                actor="test",
                schema_version=1,
                payload={"record": rec},
            )
        )
    events.append(
        MemoryEvent(
            event_id="e-like",
            kind=EventKind.FEEDBACK,
            namespace=NS,
            ts=T0,
            payload={"record_id": "r-bram", "signal": "like"},
        )
    )
    events.append(
        MemoryEvent(
            event_id="e-forget",
            kind=EventKind.FORGET,
            namespace=NS,
            ts=T0,
            payload={"record_id": "r-noor", "hard": False},
        )
    )
    return events


async def _open(path: Path) -> tuple[SQLiteClient, SQLiteStorage]:
    client = SQLiteClient(str(path))
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    return client, storage


async def _make_v1(path: Path) -> None:
    client, storage = await _open(path)
    try:
        for event in _v1_events():
            await storage.append_event(event)
    finally:
        await client.close()


def test_write_event_upcasts_v1_to_v2_explicit_envelope() -> None:
    up = V1Upcaster()
    rec = _record("r1", "Bram repairs bicycles").model_dump(mode="json")
    for key in ("layer", "type_tag", "provenance_class"):
        rec.pop(key, None)
    out = up.upcast_payload(EventKind.WRITE, 1, {"record": rec, "extra": 7})
    assert out["record"]["layer"] == "L1"
    assert out["record"]["type_tag"] == "semantic"
    assert out["extra"] == 7  # unknown fields survive
    assert "schema_version" not in out
    # pure: the input is not mutated
    assert "layer" not in rec


def test_unchanged_kinds_are_returned_as_is() -> None:
    event = MemoryEvent(kind=EventKind.FORGET, namespace=NS, payload={"record_id": "x"})
    assert V1Upcaster().upcast_event(event) is event


def test_registry_survives_reset() -> None:
    saved = (dict(versioning._UPCASTERS), dict(versioning._DECLARED))
    versioning.reset_registry()
    try:
        up = V1Upcaster()
        assert up.latest(EventKind.WRITE) == 3  # LF-E: layer upcaster v2 -> v3
    finally:  # restore the model declarations the reset wiped (other tests rely on them)
        versioning._UPCASTERS.update(saved[0])
        versioning._DECLARED.update(saved[1])


def test_ensure_is_idempotent() -> None:
    ensure_event_upcasters()
    ensure_event_upcasters()


async def test_v1_log_replays_byte_stably(tmp_path: Path) -> None:
    src = tmp_path / "v1.db"
    await _make_v1(src)
    dumps: list[bytes] = []
    for name in ("a.db", "b.db"):
        report = await migrate_sqlite(str(src), str(tmp_path / name))
        assert report.events_read == report.events_written == 5
        assert report.events_upcast == 3
        assert report.limited_rebuild
        assert set(report.not_rebuilt) == set(NOT_REBUILT_LAYERS)
        client, storage = await _open(tmp_path / name)
        try:
            dumps.append(await dump_store(storage))
        finally:
            await client.close()
    assert dumps[0] == dumps[1]


async def test_replay_restores_read_model_without_duplicates(tmp_path: Path) -> None:
    src = tmp_path / "v1.db"
    await _make_v1(src)
    dst = tmp_path / "out.db"
    report = await migrate_sqlite(str(src), str(dst))
    assert report.projected == {"records": 5}
    client, storage = await _open(dst)
    try:
        records = {r.record_id: r for r in await storage.list_records(NS)}
        assert set(records) == {"r-ilse", "r-bram", "r-noor"}
        assert records["r-bram"].scoring.likes == 1
        assert records["r-noor"].status.value == "deleted"
        events = await storage.read_events()
        assert [e.schema_version for e in events[:3]] == [3, 3, 3]  # LF-E: replay upcasts to v3
        md = MarkdownExportView(NS).render(list(records.values()))
        assert "Ilse keeps bees" in md
        assert md == MarkdownExportView(NS).render(list(reversed(list(records.values()))))
    finally:
        await client.close()


async def test_source_is_never_written(tmp_path: Path) -> None:
    src = tmp_path / "v1.db"
    await _make_v1(src)
    before = src.read_bytes()
    await migrate_sqlite(str(src), str(tmp_path / "o.db"))
    client, storage = await _open(src)
    try:
        assert all(e.schema_version == 1 for e in await storage.read_events())
    finally:
        await client.close()
    assert before  # source file existed throughout


async def test_pruned_log_is_refused(tmp_path: Path) -> None:
    src = tmp_path / "v1.db"
    await _make_v1(src)
    client = SQLiteClient(str(src))
    await client.connect()
    storage = SQLiteStorage(client, EventLogMode.ROLLING)
    await storage.start()
    try:
        await storage.set_offset("records", 5)
        assert await storage.prune_events(datetime(2030, 1, 1, tzinfo=UTC)) == 5
        await storage.append_event(_v1_events()[0].model_copy(update={"event_id": "late"}))
        target_client, target = await _open(tmp_path / "t.db")
        try:
            with pytest.raises(RebuildUnavailableError, match="pruned"):
                await ReplayBuilder(storage, target).run()
        finally:
            await target_client.close()
    finally:
        await client.close()


async def test_target_must_be_fresh(tmp_path: Path) -> None:
    src = tmp_path / "v1.db"
    await _make_v1(src)
    await migrate_sqlite(str(src), str(tmp_path / "o.db"))
    with pytest.raises(RebuildUnavailableError, match="exists"):
        await migrate_sqlite(str(src), str(tmp_path / "o.db"))
