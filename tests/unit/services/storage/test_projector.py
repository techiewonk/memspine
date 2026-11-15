"""RecordProjector delta gate (E1): lifecycle patches only, loudly filtered."""

from __future__ import annotations

import pytest

from memspine.services.storage.projector import fold_records
from memspine.store.claim_cardinality import group_key, record_group_of
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord


class MemoryStore:
    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}
        self.erased: set[str] = set()
        self.hidden: set[tuple[str, str]] = set()

    async def upsert_record(self, record: MemoryRecord) -> None:
        self.records[record.record_id] = record

    async def put_record(self, record: MemoryRecord) -> None:
        await self.upsert_record(record)

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        return self.records.get(record_id)

    async def delete_record(self, record_id: str) -> None:
        self.records.pop(record_id, None)

    async def delete_all_records(self) -> None:
        self.records.clear()

    async def group_page(
        self, namespace: str, group_key_value: str, *, after_id: str | None, limit: int
    ) -> list[MemoryRecord]:
        rows = [
            row
            for row in self.records.values()
            if row.namespace == namespace
            and (group := record_group_of(row)) is not None
            and group_key(group) == group_key_value
            and (after_id is None or row.record_id > after_id)
        ]
        return sorted(rows, key=lambda row: row.record_id)[:limit]

    async def erasure_state(self, namespace: str, record_id: str) -> tuple[bool, bool]:
        key = namespace, record_id
        return record_id in self.erased, key in self.hidden

    async def mark_erased(self, namespace: str, record_id: str) -> None:
        self.erased.add(record_id)

    async def mark_hidden(self, namespace: str, record_id: str) -> None:
        self.hidden.add((namespace, record_id))

    async def release_hidden(self, namespace: str, record_id: str) -> None:
        self.hidden.discard((namespace, record_id))


class _ReducerHarness:
    """Explicit reference view for reducer unit tests; no native event claim."""

    def __init__(self, store: MemoryStore) -> None:
        self.store = store

    async def apply(self, event: MemoryEvent) -> None:
        await fold_records(self.store, [event])


async def test_delta_may_patch_lifecycle_fields() -> None:
    store = MemoryStore()
    projector = _ReducerHarness(store)
    record = MemoryRecord(namespace="ns", memory_type="working", content="x")
    store.records[record.record_id] = record
    await projector.apply(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace="ns",
            actor="system",
            payload={
                "record_id": record.record_id,
                "set": {"tier": "warm", "skill_stage": None},
                "transition": "hot->warm",
                "reason": "idle",
            },
        )
    )
    assert store.records[record.record_id].tier == "warm"


async def test_illegal_delta_keys_are_dropped_not_applied() -> None:
    """A delta that tries to launder trust or rewrite content past the firewall
    is filtered: the legal keys apply, the illegal ones are ignored loudly."""
    store = MemoryStore()
    projector = _ReducerHarness(store)
    record = MemoryRecord(namespace="ns", memory_type="semantic", content="original", trust=0.3)
    store.records[record.record_id] = record
    await projector.apply(
        MemoryEvent(
            kind=EventKind.DECAY_TRANSITION,
            namespace="ns",
            actor="system",
            payload={
                "record_id": record.record_id,
                "set": {"tier": "warm", "trust": 1.0, "entity": "hijacked"},
                "transition": "hot->warm",
                "reason": "idle",
            },
        )
    )
    patched = store.records[record.record_id]
    assert patched.tier == "warm"  # legal key applied
    assert patched.trust == 0.3  # illegal keys dropped
    assert patched.entity is None
    assert patched.content == "original"


def _transition(record_id: str, change: dict[str, object]) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.DECAY_TRANSITION,
        namespace="ns",
        actor="system",
        payload={"record_id": record_id, "set": change, "transition": "t", "reason": "r"},
    )


async def test_delta_cannot_drop_the_taint_archived_tag() -> None:
    """B-8: tags only grow through a delta; an empty list removes nothing."""
    store = MemoryStore()
    projector = _ReducerHarness(store)
    record = MemoryRecord(namespace="ns", memory_type="semantic", content="x", tags=["a"])
    store.records[record.record_id] = record
    await projector.apply(_transition(record.record_id, {"tags_add": ["taint_archived"]}))
    assert store.records[record.record_id].tags == ["a", "taint_archived"]
    await projector.apply(_transition(record.record_id, {"tags": []}))
    assert store.records[record.record_id].tags == ["a", "taint_archived"]
    await projector.apply(_transition(record.record_id, {"tags_add": ["a", "b"]}))
    assert store.records[record.record_id].tags == ["a", "taint_archived", "b"]


async def test_legacy_tags_delta_replays_as_a_union() -> None:
    """B-8: a pre-B-8 log carries ``tags`` = old tags + the mark; replay agrees."""
    store = MemoryStore()
    projector = _ReducerHarness(store)
    record = MemoryRecord(namespace="ns", memory_type="semantic", content="x", tags=["a"])
    store.records[record.record_id] = record
    await projector.apply(_transition(record.record_id, {"tags": ["a", "taint_archived"]}))
    assert store.records[record.record_id].tags == ["a", "taint_archived"]


async def test_source_seq_is_event_derived_stable_for_metadata_and_replay() -> None:
    store = MemoryStore()
    projector = _ReducerHarness(store)
    record = MemoryRecord(namespace="ns", memory_type="episodic", content="same")

    async def write(seq: int, source: MemoryRecord) -> None:
        await projector.apply(
            MemoryEvent(
                seq=seq,
                kind=EventKind.WRITE,
                namespace="ns",
                actor="user",
                payload={"record": source.model_dump(mode="json")},
            )
        )

    await write(7, record)
    assert store.records[record.record_id].fingerprint.source_seq == 7

    # Caller-provided projection metadata is ignored; a same-snapshot metadata rewrite retains
    # the original acceptance cutoff instead of moving it to the rewrite event.
    spoofed = record.model_copy(
        update={
            "fingerprint": record.fingerprint.model_copy(update={"source_seq": 99}),
            "source": record.source.model_copy(update={"message": {"metadata": {"edit": 1}}}),
        }
    )
    await write(11, spoofed)
    assert store.records[record.record_id].fingerprint.source_seq == 7

    changed = record.model_copy(update={"content": "changed", "version": 2})
    await write(15, changed)
    assert store.records[record.record_id].fingerprint.source_seq == 15

    replay = MemoryStore()
    replay_projector = _ReducerHarness(replay)
    for seq, source in ((7, record), (11, spoofed), (15, changed)):
        await replay_projector.apply(
            MemoryEvent(
                seq=seq,
                kind=EventKind.WRITE,
                namespace="ns",
                actor="user",
                payload={"record": source.model_dump(mode="json")},
            )
        )
    assert replay.records[record.record_id].fingerprint.source_seq == 15


async def test_metadata_rewrite_does_not_infer_missing_source_seq() -> None:
    store = MemoryStore()
    projector = _ReducerHarness(store)
    record = MemoryRecord(namespace="ns", memory_type="episodic", content="same")
    await projector.apply(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="ns",
            actor="user",
            payload={"record": record.model_dump(mode="json")},
        )
    )
    later = MemoryEvent(
        seq=11,
        kind=EventKind.WRITE,
        namespace="ns",
        actor="user",
        payload={"record": record.model_dump(mode="json")},
    )
    await projector.apply(later)
    assert store.records[record.record_id].fingerprint.source_seq is None


async def test_hard_erasure_fence_suppresses_already_canonical_rewrite() -> None:
    store = MemoryStore()
    record = MemoryRecord(namespace="ns", memory_type="semantic", content="erased text")
    write = MemoryEvent(
        seq=1,
        kind=EventKind.WRITE,
        namespace="ns",
        actor="user",
        payload={"record": record.model_dump(mode="json")},
    )
    hard = MemoryEvent(
        seq=2,
        kind=EventKind.FORGET,
        namespace="ns",
        actor="user",
        payload={"record_id": record.record_id, "hard": True},
    )
    await fold_records(store, [write, hard])
    assert record.record_id not in store.records
    assert record.record_id in store.erased
    await fold_records(store, [write.model_copy(update={"seq": 3})])
    assert record.record_id not in store.records

    # Resetting a records projection keeps the append fence. Replaying an older
    # unsanitized WRITE before its FORGET cannot expose the erased text.
    await store.delete_all_records()
    await fold_records(store, [write])
    assert record.record_id not in store.records

    moved = record.model_copy(update={"namespace": "other"})
    other_namespace = write.model_copy(
        update={
            "namespace": "other",
            "seq": 4,
            "payload": {"record": moved.model_dump(mode="json")},
        }
    )
    await fold_records(store, [other_namespace])
    assert record.record_id not in store.records


async def test_cross_namespace_write_and_delta_are_rejected() -> None:
    store = MemoryStore()
    record = MemoryRecord(namespace="other", memory_type="semantic", content="x")
    bad_write = MemoryEvent(
        seq=1,
        kind=EventKind.WRITE,
        namespace="ns",
        actor="user",
        payload={"record": record.model_dump(mode="json")},
    )
    with pytest.raises(ValueError, match="namespace"):
        await fold_records(store, [bad_write])
    store.records[record.record_id] = record
    with pytest.raises(ValueError, match="namespace"):
        await fold_records(store, [_transition(record.record_id, {"status": "archived"})])
    assert store.records[record.record_id].status.value == "activated"
