"""RM-34: link projection respects canonical event order across Engines."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import create_async_engine

from memspine.memories.associative import entity_index as module
from memspine.memories.associative.entities import canonical_entity, name_fingerprint
from memspine.memories.associative.entity_index import (
    EntityIndexProjectionIncomplete,
    EntityIndexProjector,
    alias_event,
    entity_link_event,
    merge_event,
    scrub_alias_marker,
)
from memspine.services.storage.entity_index_tables import (
    EntityIndexRow,
    SqlEntityIndexStore,
    entity_index_metadata,
)
from memspine.services.storage.entity_projection_memory import MemoryEntityIndexProjection
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord
from memspine.store.replay import catch_up


class IndexStorage(MemoryEntityIndexProjection):
    def stage(self, *canonical: MemoryEvent) -> None:
        by_seq = {event.seq: event for event in canonical}
        assert None not in by_seq
        for seq in range(1, max(by_seq, default=0) + 1):
            event = by_seq.get(seq) or MemoryEvent(
                kind=EventKind.MARKER, namespace="default", payload={"marker": "noop"}
            )
            appended = self.append(event.model_copy(update={"seq": None}))
            assert appended.seq == seq

    async def seed_evidence(self, record_id: str = "evidence") -> None:
        self.records[record_id] = MemoryRecord(
            record_id=record_id, namespace="default", memory_type="semantic", content="evidence"
        )
        await self.index.put(
            [
                EntityIndexRow(
                    "default",
                    "source_state",
                    record_id,
                    "",
                    data={"live": True, "status": "activated", "gone": False},
                    seq=2,
                )
            ]
        )


def _write_entity(entity_id: str, seq: int, *, name: str = "Alex") -> MemoryEvent:
    record = MemoryRecord(namespace="default", memory_type="semantic", content=name, entity=name)
    return MemoryEvent(
        kind=EventKind.WRITE,
        namespace="default",
        payload={
            "record": record.model_dump(mode="json"),
            "entity_links": [
                {
                    "entity_id": entity_id,
                    "name_fp": name_fingerprint(canonical_entity(name)),
                    "basis": "new",
                }
            ],
        },
        seq=seq,
    )


def _entity(entity_id: str, seq: int) -> EntityIndexRow:
    return EntityIndexRow(
        "default", "entity", entity_id, entity_id, data={"name": "Alex", "basis": "new"}, seq=seq
    )


async def _link_seq(storage: IndexStorage) -> int | None:
    rows = await storage.index.select("default", "merge")
    assert len(rows) <= 1
    return rows[0].seq if rows else None


@pytest.mark.asyncio
async def test_link_marker_cannot_use_entity_created_after_its_event() -> None:
    storage = IndexStorage()
    projector = EntityIndexProjector(storage)
    # A peer projected seq 4 before this process applied marker seq 3.
    await storage.index.put([_entity("keeper", 1), _entity("newer", 4)])
    early = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 3})
    late = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 5})
    storage.stage(early, late)
    await projector.apply(early)
    assert await _link_seq(storage) is None
    await projector.apply(late)
    assert await _link_seq(storage) == 5


@pytest.mark.asyncio
async def test_duplicate_eligible_link_keeps_first_canonical_event() -> None:
    storage = IndexStorage()
    projector = EntityIndexProjector(storage)
    await storage.index.put([_entity("keeper", 1), _entity("newer", 2)])
    first = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 3})
    second = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 4})
    storage.stage(first, second)
    # Two independent projectors can receive committed events in reverse apply order.
    await projector.apply(second)
    await projector.apply(first)
    assert await _link_seq(storage) == 3


@pytest.mark.asyncio
async def test_older_link_followed_by_confirmed_merge_matches_canonical_replay() -> None:
    first, replayed = IndexStorage(), IndexStorage()
    for storage in (first, replayed):
        await storage.index.put([_entity("keeper", 1), _entity("newer", 2)])
    link = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 3})
    confirmed = merge_event("default", "keeper", "newer", ["evidence"]).model_copy(
        update={"seq": 4}
    )
    for storage in (first, replayed):
        await storage.seed_evidence()
        storage.stage(link, confirmed)
    await EntityIndexProjector(first).apply(confirmed)
    await EntityIndexProjector(first).apply(link)
    replay_projector = EntityIndexProjector(replayed)
    await replay_projector.apply(link)
    await replay_projector.apply(confirmed)
    assert await first.index.select("default", "merge") == await replayed.index.select(
        "default", "merge"
    )
    rows = await first.index.select("default", "merge")
    assert {(row.ref, row.seq) for row in rows} == {("newer", 3), ("newer", 4)}


@pytest.mark.asyncio
async def test_competing_link_targets_keep_first_canonical_event() -> None:
    first, replayed = IndexStorage(), IndexStorage()
    for storage in (first, replayed):
        await storage.index.put(
            [_entity("keeper-a", 1), _entity("keeper-b", 1), _entity("newer", 2)]
        )
    early = entity_link_event("default", "newer", "keeper-a").model_copy(update={"seq": 3})
    late = entity_link_event("default", "newer", "keeper-b").model_copy(update={"seq": 4})
    first.stage(early, late)
    replayed.stage(early, late)
    live_projector = EntityIndexProjector(first)
    await live_projector.apply(late)
    await live_projector.apply(early)
    replay_projector = EntityIndexProjector(replayed)
    await replay_projector.apply(early)
    await replay_projector.apply(late)
    assert await first.index.select("default", "merge") == await replayed.index.select(
        "default", "merge"
    )


@pytest.mark.asyncio
async def test_historical_name_match_ignores_future_merge() -> None:
    storage = IndexStorage()
    await storage.index.put(
        [
            _entity("alex", 1),
            _entity("dan", 2),
            _entity("deferred", 5),
            EntityIndexRow("default", "name", "alex", "alex", "name", seq=1),
            EntityIndexRow("default", "name", "alex", "deferred", "name", seq=5),
            EntityIndexRow(
                "default", "merge", "future", "dan", "alex", data={"evidence": ["e"]}, seq=6
            ),
        ]
    )
    outcome, target, candidates = await module._match(
        storage.index, "default", "alex", None, before_seq=5, exclude="deferred"
    )
    assert (outcome, target, candidates) == ("unique", "alex", [])


@pytest.mark.asyncio
async def test_link_projected_before_its_entity_writes_recovers_to_replay() -> None:
    live, replayed = IndexStorage(), IndexStorage()
    marker = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 3})
    for storage in (live, replayed):
        storage.stage(_write_entity("keeper", 1), _write_entity("newer", 2), marker)
    await EntityIndexProjector(live).apply(marker)
    await EntityIndexProjector(replayed).apply(marker)
    assert await live.index.select("default", "merge") == await replayed.index.select(
        "default", "merge"
    )


@pytest.mark.asyncio
async def test_erased_earlier_merge_still_suppresses_intervening_link() -> None:
    live, replayed = IndexStorage(), IndexStorage()
    for storage in (live, replayed):
        await storage.index.put([_entity("keeper", 1), _entity("newer", 2)])
    merge = merge_event("default", "keeper", "newer", ["evidence"]).model_copy(update={"seq": 3})
    intervening = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 4})
    forgotten = MemoryEvent(
        kind=EventKind.FORGET, namespace="default", payload={"record_id": "evidence"}, seq=5
    )
    after_undo = entity_link_event("default", "newer", "keeper").model_copy(update={"seq": 6})
    for storage in (live, replayed):
        await storage.seed_evidence()
        storage.stage(merge, intervening, forgotten, after_undo)
    live_projector = EntityIndexProjector(live)
    with pytest.raises(EntityIndexProjectionIncomplete):
        await live_projector.apply(merge)
    for event in (forgotten, intervening, after_undo):
        await live_projector.apply(event)
    replay_projector = EntityIndexProjector(replayed)
    with pytest.raises(EntityIndexProjectionIncomplete):
        await replay_projector.apply(merge)
    with pytest.raises(EntityIndexProjectionIncomplete):
        await replay_projector.apply(intervening)
    for event in (forgotten, after_undo):
        await replay_projector.apply(event)
    assert await live.index.select("default", "merge") == await replayed.index.select(
        "default", "merge"
    )
    rows = await live.index.select("default", "merge")
    assert [(row.ref, row.seq) for row in rows] == [("newer", 6)]


@pytest.mark.asyncio
async def test_pending_alias_pins_history_until_lagging_record_arrives() -> None:
    storage = IndexStorage()
    evidence = MemoryRecord(
        record_id="evidence", namespace="default", memory_type="semantic", content="evidence"
    )
    write = MemoryEvent(
        kind=EventKind.WRITE,
        namespace="default",
        payload={"record": evidence.model_dump(mode="json")},
    )
    alias = alias_event("default", "keeper", "Alexander", ["evidence"])
    storage.stage(
        _write_entity("keeper", 1),
        write.model_copy(update={"seq": 2}),
        alias.model_copy(update={"seq": 3}),
    )
    storage.records.pop("evidence")  # Peer record projector has not materialized WRITE 2.
    projector = EntityIndexProjector(storage)
    with pytest.raises(EntityIndexProjectionIncomplete):
        await projector.apply(alias.model_copy(update={"seq": 3}))
    assert await storage.index.select("default", "name", key="alexander") == []
    assert await storage.index.select("default", "pending_positive", key="3")
    assert await storage.get_offset("entity_index") == 2

    storage.records["evidence"] = evidence
    receipt = await projector.advance_to("default", 3)  # No new canonical event.
    assert receipt.complete and receipt.namespace_cursor == 3
    assert await storage.get_offset("entity_index") == 3
    assert len(await storage.index.select("default", "name", key="alexander")) == 1
    assert await storage.index.select("default", "pending_positive") == []


@pytest.mark.asyncio
async def test_pending_alias_scans_later_forget_before_certifying(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(module, "_CATCH_UP_PAGE", 1)
    monkeypatch.setattr(module, "_CATCH_UP_PAGES", 1)
    storage = IndexStorage()
    evidence = MemoryRecord(
        record_id="evidence", namespace="default", memory_type="semantic", content="evidence"
    )
    write = MemoryEvent(
        kind=EventKind.WRITE,
        namespace="default",
        payload={"record": evidence.model_dump(mode="json")},
    )
    alias = alias_event("default", "keeper", "Alexander", ["evidence"])
    forget = MemoryEvent(
        kind=EventKind.FORGET, namespace="default", payload={"record_id": "evidence"}
    )
    storage.stage(
        _write_entity("keeper", 1),
        write.model_copy(update={"seq": 2}),
        alias.model_copy(update={"seq": 3}),
        forget.model_copy(update={"seq": 4}),
    )
    projector = EntityIndexProjector(storage)
    for _ in range(3):
        with pytest.raises(EntityIndexProjectionIncomplete):
            await projector.apply(alias.model_copy(update={"seq": 3}))
    assert await storage.get_offset("entity_index") == 2
    assert await storage.index.select("default", "pending_positive", key="3")
    receipt = await projector.advance_to("default", 4)
    assert receipt.complete and receipt.namespace_cursor == 4
    assert await storage.get_offset("entity_index") >= 3
    await projector.advance_to("default", 4)
    assert await storage.get_offset("entity_index") == 4
    assert await storage.index.select("default", "pending_positive") == []
    assert await storage.index.select("default", "name", key="alexander") == []


@pytest.mark.asyncio
async def test_ordered_cursor_never_regresses_for_old_target() -> None:
    storage = IndexStorage()
    storage.stage(_write_entity("keeper", 1), _write_entity("newer", 2))
    projector = EntityIndexProjector(storage)
    high = await projector.advance_to("default", 2)
    low = await projector.advance_to("default", 1)
    assert high.namespace_cursor == low.namespace_cursor == 2
    assert high.global_offset == low.global_offset == 2


@pytest.mark.asyncio
async def test_serialized_purge_blocks_delayed_older_write() -> None:
    storage = IndexStorage()
    write = _write_entity("keeper", 1)
    storage.stage(write)
    record_id = str(write.payload["record"]["record_id"])
    projector = EntityIndexProjector(storage)
    await projector.purge_records("default", [record_id])
    assert await storage.index.select("default", "purge_guard", key=record_id)
    await projector.apply(write)
    assert await storage.index.select("default", "entity") == []
    assert await storage.index.select("default", "name") == []


@pytest.mark.asyncio
async def test_replay_scans_past_pending_marker_to_later_forget() -> None:
    storage = IndexStorage()
    evidence = MemoryRecord(
        record_id="evidence", namespace="default", memory_type="semantic", content="evidence"
    )
    write = MemoryEvent(
        kind=EventKind.WRITE,
        namespace="default",
        payload={"record": evidence.model_dump(mode="json")},
        seq=2,
    )
    alias = alias_event("default", "keeper", "Alexander", ["evidence"]).model_copy(
        update={"seq": 3}
    )
    forgotten = MemoryEvent(
        kind=EventKind.FORGET,
        namespace="default",
        payload={"record_id": "evidence"},
        seq=4,
    )
    storage.stage(_write_entity("keeper", 1), write, alias, forgotten)
    projector = EntityIndexProjector(storage)
    await catch_up(storage, [projector])
    assert await storage.index.select("default", "pending_positive") == []
    assert await storage.index.select("default", "name", key="alexander") == []
    assert (await projector.advance_to("default", 4)).complete


@pytest.mark.asyncio
async def test_direct_purge_recertifies_pending_reference_cursor() -> None:
    storage = IndexStorage()
    evidence = MemoryRecord(
        record_id="evidence", namespace="default", memory_type="semantic", content="evidence"
    )
    write = MemoryEvent(
        kind=EventKind.WRITE,
        namespace="default",
        payload={"record": evidence.model_dump(mode="json")},
        seq=2,
    )
    alias = alias_event("default", "keeper", "Alexander", ["evidence"]).model_copy(
        update={"seq": 3}
    )
    storage.stage(_write_entity("keeper", 1), write, alias)
    storage.records.pop("evidence")
    projector = EntityIndexProjector(storage)
    with pytest.raises(EntityIndexProjectionIncomplete):
        await projector.apply(alias)
    assert await storage.get_offset("entity_index") == 2
    await projector.purge_records("default", ["evidence"])
    assert await storage.index.select("default", "pending_positive") == []
    assert await storage.get_offset("entity_index") == 3
    assert (await projector.advance_to("default", 3)).complete


@pytest.mark.asyncio
async def test_hard_forget_cancels_pending_alias_before_marker_scrub() -> None:
    storage = IndexStorage()
    evidence_a = MemoryRecord(
        record_id="a", namespace="default", memory_type="semantic", content="evidence a"
    )
    evidence_b = MemoryRecord(
        record_id="b", namespace="default", memory_type="semantic", content="evidence b"
    )

    def write(record: MemoryRecord, seq: int) -> MemoryEvent:
        return MemoryEvent(
            kind=EventKind.WRITE,
            namespace="default",
            payload={"record": record.model_dump(mode="json")},
            seq=seq,
        )

    alias = alias_event("default", "keeper", "Secret Alias", ["a", "b"]).model_copy(
        update={"seq": 4}
    )
    hard_forget = MemoryEvent(
        kind=EventKind.FORGET,
        namespace="default",
        payload={"record_id": "a", "hard": True},
        seq=5,
    )
    storage.stage(
        _write_entity("keeper", 1),
        write(evidence_a, 2),
        write(evidence_b, 3),
        alias,
        hard_forget,
    )
    storage.records.pop("b")  # The second citation's record projector is late.
    projector = EntityIndexProjector(storage)
    with pytest.raises(EntityIndexProjectionIncomplete):
        await projector.apply(alias)
    assert await storage.index.select("default", "pending_positive", key="4")
    await projector.apply(hard_forget)
    assert await storage.index.select("default", "pending_positive") == []
    assert await storage.index.select("default", "name", key="secret alias") == []

    storage.records["b"] = evidence_b  # The peer catches up before canonical scrub.
    await projector.advance_to("default", 5)
    assert await storage.index.select("default", "name", key="secret alias") == []
    scrubbed = dict(storage.events[3].payload)
    assert scrub_alias_marker(scrubbed, {"a"})
    storage.events[3] = storage.events[3].model_copy(update={"payload": scrubbed})
    await projector.advance_to("default", 5)
    assert await storage.index.select("default", "name", key="secret alias") == []


@pytest.mark.asyncio
async def test_sql_link_upsert_keeps_first_seq_across_independent_stores(tmp_path: Path) -> None:
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'index.db'}")
    try:
        async with engine.begin() as conn:
            await conn.run_sync(entity_index_metadata.create_all)
        first, second = SqlEntityIndexStore(engine), SqlEntityIndexStore(engine)
        high = EntityIndexRow(
            "default",
            "merge",
            "same-link",
            "keeper",
            "newer",
            data={"link": True, "basis": "unique_name"},
            seq=4,
        )
        low = EntityIndexRow(
            "default",
            "merge",
            "same-link",
            "keeper",
            "newer",
            data={"link": True, "basis": "unique_name"},
            seq=3,
        )
        await asyncio.gather(first.put([high]), second.put([low]))
        rows = await first.select("default", "merge", key="same-link")
        assert [row.seq for row in rows] == [3]
        await second.put([high])
        rows = await first.select("default", "merge", key="same-link")
        assert [row.seq for row in rows] == [3]
        competing_late = EntityIndexRow(
            "default", "merge", "target-b", "other", "competing", data={"link": True}, seq=6
        )
        competing_early = EntityIndexRow(
            "default", "merge", "target-a", "keeper", "competing", data={"link": True}, seq=5
        )
        await asyncio.gather(first.put([competing_late]), second.put([competing_early]))
        rows = await first.select("default", "merge", ref="competing")
        assert [(row.key, row.seq) for row in rows] == [("target-a", 5)]
        cited = EntityIndexRow(
            "default", "merge", "cited", "keeper", "competing", data={"evidence": ["r"]}, seq=7
        )
        await second.put([cited])
        await first.put([competing_late])
        rows = await first.select("default", "merge", ref="competing")
        assert {(row.key, row.seq) for row in rows} == {("target-a", 5), ("cited", 7)}
    finally:
        await engine.dispose()
