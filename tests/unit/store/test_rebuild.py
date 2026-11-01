"""``memspine.store.rebuild``: replay of the retained log into a fresh store."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from structlog.testing import capture_logs

from memspine.clients.sqlite import SQLiteClient, SQLiteSpec
from memspine.exceptions import RebuildUnavailableError
from memspine.observability.logging import EVENT_REBUILD
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.store.events import EventKind, EventLogMode, MemoryEvent
from memspine.store.rebuild import (
    NOT_REBUILT_LAYERS,
    ReplayBuilder,
    ReplayReport,
    dump_store,
    replay_sqlite,
)


async def _store(path: Path, mode: EventLogMode = EventLogMode.FULL) -> SQLiteStorage:
    client = SQLiteClient(SQLiteSpec(str(path)))
    await client.open()
    store = SQLiteStorage(client, mode=mode)
    await store.start()
    return store


def _marker(n: int) -> MemoryEvent:
    return MemoryEvent(kind=EventKind.MARKER, namespace="n", payload={"marker": f"m{n}"})


@pytest.fixture
async def source(tmp_path: Path) -> AsyncIterator[SQLiteStorage]:
    store = await _store(tmp_path / "src.db")
    for n in range(3):
        await store.append_event(_marker(n))
    yield store
    await store.stop()


def test_report_lists_the_layers_a_replay_does_not_rebuild() -> None:
    report = ReplayReport(by_kind={"b": 1, "a": 2}, projected={"y": 1, "x": 0})
    out = report.as_dict()
    assert list(out["by_kind"]) == ["a", "b"] and list(out["projected"]) == ["x", "y"]
    assert out["not_rebuilt"] == NOT_REBUILT_LAYERS and out["limited_rebuild"] is True
    report.not_rebuilt.pop("vector")
    assert "vector" in NOT_REBUILT_LAYERS  # each report owns its copy


async def test_replay_copies_every_event_and_logs_memory_rebuild(
    source: SQLiteStorage, tmp_path: Path
) -> None:
    target = await _store(tmp_path / "dst.db")
    try:
        with capture_logs() as logs:
            report = await ReplayBuilder(source, target, batch_size=2).run()
        assert report.events_read == report.events_written == 3
        assert report.by_kind == {EventKind.MARKER.value: 3}
        moved = await target.read_events(after_seq=0, limit=10)
        assert [e.payload["marker"] for e in moved] == ["m0", "m1", "m2"]
        assert [e.seq for e in moved] == [1, 2, 3]
        (line,) = [entry for entry in logs if entry["event"] == EVENT_REBUILD]
        assert line["log_level"] == "info"
        assert line["events_read"] == 3 and line["events_written"] == 3
        assert line["by_kind"] == {EventKind.MARKER.value: 3}
        assert line["projected"] == dict(sorted(report.projected.items()))
    finally:
        await target.stop()


async def test_replay_refuses_a_target_that_holds_events(
    source: SQLiteStorage, tmp_path: Path
) -> None:
    target = await _store(tmp_path / "dst.db")
    try:
        await target.append_event(_marker(9))
        with capture_logs() as logs, pytest.raises(RebuildUnavailableError, match="not fresh"):
            await ReplayBuilder(source, target).run()
        assert not [entry for entry in logs if entry["event"] == EVENT_REBUILD]
    finally:
        await target.stop()


async def test_replay_refuses_an_ephemeral_source(tmp_path: Path) -> None:
    source = await _store(tmp_path / "eph.db", EventLogMode.EPHEMERAL)
    target = await _store(tmp_path / "dst.db")
    try:
        with pytest.raises(RebuildUnavailableError, match="ephemeral"):
            await ReplayBuilder(source, target).run()
    finally:
        await source.stop()
        await target.stop()


async def test_dump_store_is_deterministic_and_survives_replay(
    source: SQLiteStorage, tmp_path: Path
) -> None:
    first = await dump_store(source)
    assert first == await dump_store(source)
    assert b'"m2"' in first
    target = await _store(tmp_path / "dst.db")
    try:
        await ReplayBuilder(source, target).run()
        # event ids and payloads are copied unchanged, so the replica dumps the same
        assert await dump_store(target) == first
    finally:
        await target.stop()


async def test_replay_sqlite_refuses_an_existing_target(tmp_path: Path) -> None:
    existing = tmp_path / "taken.db"
    existing.write_bytes(b"")
    with pytest.raises(RebuildUnavailableError, match="fresh store"):
        await replay_sqlite(str(tmp_path / "src.db"), str(existing))
