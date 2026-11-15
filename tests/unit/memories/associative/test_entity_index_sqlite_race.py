"""RM-145: same-name writes through independent Engines on one SQLite event log."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from pathlib import Path
from typing import Any

from memspine import Engine
from memspine.memories.associative.entity_cache import entity_lookup
from memspine.memories.associative.entity_index import (
    LINK_MARKER,
    LINKS_KEY,
    entity_status,
)
from memspine.services.storage.entity_index_tables import entity_index_store_of
from memspine.store.events import EventKind, MemoryEvent


def _engine(path: Path) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"records": {"provider": "sqlite", "settings": {"path": str(path)}}},
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"hybrid": True, "record_access": False},
    )


async def test_two_engines_same_name_converges_after_interleaved_appends(tmp_path: Path) -> None:
    path = tmp_path / "same-name.db"
    a, b = _engine(path), _engine(path)
    await a.start()
    await b.start()
    assert a._storage is not None and b._storage is not None
    assert a._storage is not b._storage
    prepared = 0
    both_prepared = asyncio.Event()
    committed = 0
    both_committed = asyncio.Event()
    originals = (a._storage.append_event, b._storage.append_event)

    def pause_after_append(
        original: Callable[[MemoryEvent], Coroutine[Any, Any, MemoryEvent]],
    ) -> Callable[[MemoryEvent], Coroutine[Any, Any, MemoryEvent]]:
        async def paused(event: MemoryEvent) -> MemoryEvent:
            nonlocal prepared, committed
            if event.kind is EventKind.WRITE:
                prepared += 1
                if prepared == 2:
                    both_prepared.set()
                await asyncio.wait_for(both_prepared.wait(), 10)
            appended = await original(event)
            if event.kind is EventKind.WRITE:
                committed += 1
                if committed == 2:
                    both_committed.set()
                await asyncio.wait_for(both_committed.wait(), 10)
            return appended

        return paused

    a._storage.append_event = pause_after_append(originals[0])  # type: ignore[assignment]
    b._storage.append_event = pause_after_append(originals[1])  # type: ignore[assignment]
    try:
        first, second = await asyncio.gather(
            a.write("Alex works here", entity="Alex"),
            b.write("Alex lives here", entity="Alex"),
        )
        assert both_committed.is_set()
        events = await a._storage.read_events(0, 100)
        writes = [e for e in events if e.kind is EventKind.WRITE]
        assert len(writes) == 2
        ids = [e.payload[LINKS_KEY][0]["entity_id"] for e in writes]
        assert len(set(ids)) == 2
        assert [e.payload[LINKS_KEY][0]["basis"] for e in writes] == ["new", "new"]
        markers = [e for e in events if e.payload.get("marker") == LINK_MARKER]
        assert markers
        assert {(m.payload["entity_id"], m.payload["target"]) for m in markers} == {
            (ids[1], ids[0])
        }
        lookup = await entity_lookup(a._storage, "default")
        assert set(lookup.expand([ids[0]])) == set(ids)
        other_view = await entity_lookup(b._storage, "default")
        assert set(other_view.expand([ids[0]])) == set(ids)
        store = entity_index_store_of(a._storage)
        links = await store.select("default", "link")
        assert {r.key for r in links} == {first.record_id, second.record_id}
        assert await entity_status(a._storage, "default", ids[0]) == "provisional"
        assert await entity_status(a._storage, "default", ids[1]) == "provisional"
        third = await a.write("Alex teaches", entity="Alex")
        explicit = await b.write("Alex performs", entity="Alex", tags=["entity_ref:new"])
        ambiguous = await a.write("Alex travels", entity="Alex")
        foreign = await b.write("Alex visits", namespace="other", entity="Alex")
        events = await a._storage.read_events(0, 100)
        by_record = {
            e.payload["record"]["record_id"]: e.payload[LINKS_KEY][0]
            for e in events
            if e.kind is EventKind.WRITE
        }
        assert by_record[third.record_id]["entity_id"] == ids[0]
        assert by_record[third.record_id]["basis"] == "unique_name"
        explicit_id = by_record[explicit.record_id]["entity_id"]
        assert explicit_id not in ids
        assert by_record[explicit.record_id]["basis"] == "explicit"
        assert by_record[ambiguous.record_id]["basis"] == "ambiguous"
        assert by_record[ambiguous.record_id]["entity_id"] not in {*ids, explicit_id}
        assert by_record[foreign.record_id]["entity_id"] not in {*ids, explicit_id}
        assert await entity_status(a._storage, "default", explicit_id) == "confirmed"
        assert set((await entity_lookup(a._storage, "default")).expand([ids[0]])) == set(ids)
        before = [
            (r.kind, r.key, r.entity_id, r.ref, r.match, r.data, r.seq)
            for kind in ("entity", "name", "link", "merge", "cite")
            for r in await store.select("default", kind)
        ]
    finally:
        a._storage.append_event, b._storage.append_event = originals  # type: ignore[method-assign]
        await a.stop()
        await b.stop()

    reopened = _engine(path)
    await reopened.start()
    try:
        assert reopened._storage is not None
        events = await reopened._storage.read_events(0, 100)
        assert {
            (e.payload["entity_id"], e.payload["target"])
            for e in events
            if e.payload.get("marker") == LINK_MARKER
        } == {(ids[1], ids[0])}
        assert len(await entity_index_store_of(reopened._storage).select("default", "link")) == 5
        await reopened.rebuild()
        events = await reopened._storage.read_events(0, 100)
        assert {
            (e.payload["entity_id"], e.payload["target"])
            for e in events
            if e.payload.get("marker") == LINK_MARKER
        } == {(ids[1], ids[0])}
        store = entity_index_store_of(reopened._storage)
        replayed = [
            (r.kind, r.key, r.entity_id, r.ref, r.match, r.data, r.seq)
            for kind in ("entity", "name", "link", "merge", "cite")
            for r in await store.select("default", kind)
        ]
        assert replayed == before, {
            "events": [(e.seq, e.kind.value, e.payload.get("marker")) for e in events],
            "live_merges": [row for row in before if row[0] == "merge"],
            "replayed_merges": [row for row in replayed if row[0] == "merge"],
        }
    finally:
        await reopened.stop()
