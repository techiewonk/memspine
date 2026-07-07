"""Prospective store (M13.8/ADR-016): builders, pending, acknowledge."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConflictError
from memspine.memories.prospective.watches import ProspectiveMemory, make_watch_record

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


class FakeStorage:
    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}
        self.events: list[MemoryEvent] = []

    async def list_records(
        self, namespace: str, memory_type: str | None = None
    ) -> list[MemoryRecord]:
        return [
            r
            for r in self.records.values()
            if r.namespace == namespace and (memory_type is None or r.memory_type == memory_type)
        ]

    async def get_record(self, record_id: str) -> MemoryRecord | None:
        return self.records.get(record_id)

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        batch = [e for e in self.events if e.seq is not None and e.seq > after_seq]
        return batch[:limit]


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def appended() -> list[MemoryEvent]:
    return []


@pytest.fixture
def store(storage: FakeStorage, appended: list[MemoryEvent]) -> ProspectiveMemory:
    async def append(event: MemoryEvent) -> None:
        appended.append(event)
        # Mirror the projector's delta merge so follow-up reads see the change.
        if event.kind is EventKind.DECAY_TRANSITION:
            record = storage.records[str(event.payload["record_id"])]
            data = record.model_dump()
            data.update(event.payload["set"])  # type: ignore[call-overload]
            storage.records[record.record_id] = MemoryRecord.model_validate(data)

    return ProspectiveMemory(storage, append)


def conflict_event(seq: int, entity: str, attribute: str, ts: datetime) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.CONFLICT,
        namespace="ns",
        seq=seq,
        ts=ts,
        payload={"verdict": "update", "action": "updated", "fact_key": [entity, attribute]},
    )


# ── make_watch_record ─────────────────────────────────────────────────────────


def test_due_watch_rides_valid_from() -> None:
    due_at = NOW + timedelta(days=1)
    record = make_watch_record("ns", "ping me", due_at=due_at)
    assert record.memory_type == "prospective"
    assert record.valid_from == due_at
    assert record.entity is None and record.attribute is None


def test_target_watch_rides_entity_attribute() -> None:
    record = make_watch_record("ns", "recheck", entity="alice", attribute="city")
    assert (record.entity, record.attribute) == ("alice", "city")
    assert record.valid_from <= datetime.now(UTC)


def test_watch_requires_exactly_one_trigger() -> None:
    with pytest.raises(ConflictError, match="needs a trigger"):
        make_watch_record("ns", "no trigger")
    with pytest.raises(ConflictError, match="not both"):
        make_watch_record("ns", "two triggers", due_at=NOW, entity="alice")
    with pytest.raises(ConflictError, match="needs its entity"):
        make_watch_record("ns", "orphan attribute", attribute="city")


def test_naive_due_at_is_refused() -> None:
    with pytest.raises(ConflictError, match="timezone-aware"):
        make_watch_record("ns", "when?", due_at=datetime(2026, 7, 8, 12, 0))


# ── pending ───────────────────────────────────────────────────────────────────


async def test_pending_fires_due_and_invalidation(
    store: ProspectiveMemory, storage: FakeStorage
) -> None:
    due = make_watch_record("ns", "due", due_at=NOW - timedelta(hours=1))
    target = make_watch_record("ns", "target", entity="alice", attribute="city").model_copy(
        update={"recorded_at": NOW - timedelta(hours=2)}
    )
    future = make_watch_record("ns", "later", due_at=NOW + timedelta(hours=1))
    for record in (due, target, future):
        storage.records[record.record_id] = record
    storage.events = [conflict_event(1, "alice", "city", NOW - timedelta(minutes=5))]

    fired = await store.pending("ns", NOW)
    assert {record.record_id for record in fired} == {due.record_id, target.record_id}


async def test_pending_skips_log_scan_without_target_watches(
    store: ProspectiveMemory, storage: FakeStorage
) -> None:
    due = make_watch_record("ns", "due", due_at=NOW - timedelta(hours=1))
    storage.records[due.record_id] = due

    async def explode(after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        raise AssertionError("no target watches — the log must not be scanned")

    storage.read_events = explode  # type: ignore[method-assign]
    assert [r.record_id for r in await store.pending("ns", NOW)] == [due.record_id]


async def test_ephemeral_log_degrades_invalidation_only(
    store: ProspectiveMemory, storage: FakeStorage
) -> None:
    """No readable events (event_log.mode: ephemeral): invalidation watches
    never fire, due-time watches are unaffected (ADR-016 §2)."""
    due = make_watch_record("ns", "due", due_at=NOW - timedelta(hours=1))
    target = make_watch_record("ns", "target", entity="alice", attribute="city")
    for record in (due, target):
        storage.records[record.record_id] = record
    storage.events = []  # read_events returns nothing
    fired = await store.pending("ns", NOW)
    assert [record.record_id for record in fired] == [due.record_id]


# ── acknowledge ───────────────────────────────────────────────────────────────


async def test_acknowledge_archives_via_allow_listed_delta(
    store: ProspectiveMemory, storage: FakeStorage, appended: list[MemoryEvent]
) -> None:
    record = make_watch_record("ns", "due", due_at=NOW - timedelta(hours=1))
    storage.records[record.record_id] = record

    acked = await store.acknowledge(record.record_id, namespace="ns")
    assert acked.status is RecordStatus.ARCHIVED
    assert acked.valid_to is not None

    [event] = appended
    assert event.kind is EventKind.DECAY_TRANSITION
    assert event.payload["reason"] == "watch_fired"
    assert "record" not in event.payload  # delta, never a snapshot (P3.1)
    assert set(event.payload) == {"record_id", "set", "transition", "reason"}
    # Only projector-allow-listed lifecycle fields (D-47).
    assert set(event.payload["set"]) <= {"status", "valid_to"}

    assert await store.pending("ns", NOW) == []  # acknowledged => no longer pending


async def test_acknowledge_is_idempotent(
    store: ProspectiveMemory, storage: FakeStorage, appended: list[MemoryEvent]
) -> None:
    record = make_watch_record("ns", "due", due_at=NOW - timedelta(hours=1))
    storage.records[record.record_id] = record
    await store.acknowledge(record.record_id, namespace="ns")
    count = len(appended)
    again = await store.acknowledge(record.record_id, namespace="ns")
    assert again.status is RecordStatus.ARCHIVED
    assert len(appended) == count  # no second event


async def test_acknowledge_error_shape_hides_foreign_records(
    store: ProspectiveMemory, storage: FakeStorage
) -> None:
    foreign = make_watch_record("other", "theirs", due_at=NOW)
    storage.records[foreign.record_id] = foreign
    with pytest.raises(ConflictError) as missing:
        await store.acknowledge("missing", namespace="ns")
    with pytest.raises(ConflictError) as cross:
        await store.acknowledge(foreign.record_id, namespace="ns")
    # One error message shape for missing AND foreign (ADR-014): no oracle.
    assert str(missing.value).replace("'missing'", "X") == str(cross.value).replace(
        f"'{foreign.record_id}'", "X"
    )


async def test_acknowledge_refuses_non_prospective_records(
    store: ProspectiveMemory, storage: FakeStorage
) -> None:
    fact = MemoryRecord(namespace="ns", memory_type="semantic", content="a fact")
    storage.records[fact.record_id] = fact
    with pytest.raises(ConflictError, match="not prospective"):
        await store.acknowledge(fact.record_id, namespace="ns")
