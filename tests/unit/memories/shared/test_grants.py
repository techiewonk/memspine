"""Shared-memory grants (R2/ADR-016): builders, lifecycle, scope mapping."""

from __future__ import annotations

import pytest

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.exceptions import ConflictError
from memspine.memories.shared.grants import (
    GRANT_ATTRIBUTE,
    SharedMemory,
    active_grants,
    make_grant_record,
    parse_grant,
)


class FakeStorage:
    def __init__(self) -> None:
        self.records: dict[str, MemoryRecord] = {}

    async def list_namespaces(self) -> list[str]:
        return sorted({r.namespace for r in self.records.values()})

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


@pytest.fixture
def storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture
def appended() -> list[MemoryEvent]:
    return []


@pytest.fixture
def store(storage: FakeStorage, appended: list[MemoryEvent]) -> SharedMemory:
    async def append(event: MemoryEvent) -> None:
        appended.append(event)
        # Mirror the projector so follow-up reads see the change.
        if event.kind is EventKind.WRITE:
            record = MemoryRecord.model_validate(event.payload["record"])
            storage.records[record.record_id] = record
        elif event.kind is EventKind.DECAY_TRANSITION:
            record = storage.records[str(event.payload["record_id"])]
            data = record.model_dump()
            data.update(event.payload["set"])  # type: ignore[call-overload]
            storage.records[record.record_id] = MemoryRecord.model_validate(data)

    return SharedMemory(storage, append)


# ── builders ──────────────────────────────────────────────────────────────────


def test_grant_record_shape_and_parse_round_trip() -> None:
    record = make_grant_record("team-a", "team-b", memory_types=frozenset({"semantic"}))
    assert record.namespace == "team-a"  # the grant lives with what it exposes
    assert record.memory_type == "shared"
    assert record.entity == "team-b"
    assert record.attribute == GRANT_ATTRIBUTE
    grant = parse_grant(record)
    assert grant is not None
    assert (grant.grantor, grant.grantee) == ("team-a", "team-b")
    assert grant.memory_types == frozenset({"semantic"})


def test_unscoped_grant_parses_as_every_type() -> None:
    grant = parse_grant(make_grant_record("a", "b"))
    assert grant is not None and grant.memory_types is None


def test_parse_grant_rejects_non_grant_shapes() -> None:
    fact = MemoryRecord(namespace="a", memory_type="semantic", content="x")
    assert parse_grant(fact) is None
    keyless = make_grant_record("a", "b").model_copy(update={"entity": None})
    assert parse_grant(keyless) is None


def test_active_grants_excludes_archived_and_quarantined() -> None:
    live = make_grant_record("a", "b")
    revoked = make_grant_record("a", "c").model_copy(update={"status": RecordStatus.ARCHIVED})
    held = make_grant_record("a", "d").model_copy(update={"quarantined": True})
    assert [g.grantee for g in active_grants([live, revoked, held])] == ["b"]


# ── grant / revoke lifecycle ──────────────────────────────────────────────────


async def test_grant_writes_through_the_door(
    store: SharedMemory, appended: list[MemoryEvent]
) -> None:
    record = await store.grant("a", "b", memory_types=["semantic"])
    [event] = appended
    assert event.kind is EventKind.WRITE and event.namespace == "a"
    assert event.payload["record"]["record_id"] == record.record_id


async def test_identical_regrant_is_idempotent(
    store: SharedMemory, appended: list[MemoryEvent]
) -> None:
    first = await store.grant("a", "b", memory_types=["semantic"])
    again = await store.grant("a", "b", memory_types=["semantic"])
    assert again.record_id == first.record_id
    assert len(appended) == 1  # nothing new rode the log


async def test_scope_change_supersedes_the_old_grant(
    store: SharedMemory, storage: FakeStorage
) -> None:
    first = await store.grant("a", "b", memory_types=["semantic"])
    second = await store.grant("a", "b")  # widened to every type
    old = storage.records[first.record_id]
    assert old.status is RecordStatus.ARCHIVED
    assert old.evolve_to == second.record_id
    assert await store.grants_to("b") == {"a": None}  # one live grant, new scope


async def test_revoke_archives_and_raises_when_nothing_live(
    store: SharedMemory, storage: FakeStorage
) -> None:
    granted = await store.grant("a", "b")
    revoked = await store.revoke("a", "b")
    assert revoked.record_id == granted.record_id
    assert storage.records[granted.record_id].status is RecordStatus.ARCHIVED
    with pytest.raises(ConflictError, match="no active grant"):
        await store.revoke("a", "b")


async def test_grant_validation(store: SharedMemory) -> None:
    with pytest.raises(ConflictError, match="itself"):
        await store.grant("a", "a")
    with pytest.raises(ConflictError, match="unknown memory type"):
        await store.grant("a", "b", memory_types=["telepathic"])
    with pytest.raises(ConflictError, match="never cross"):
        await store.grant("a", "b", memory_types=["shared"])
    with pytest.raises(ConflictError, match="grants nothing"):
        await store.grant("a", "b", memory_types=[])


# ── scope mapping ─────────────────────────────────────────────────────────────


async def test_grants_to_maps_grantor_to_scope(store: SharedMemory) -> None:
    await store.grant("a", "reader", memory_types=["semantic", "episodic"])
    await store.grant("b", "reader")
    await store.grant("c", "somebody-else")
    assert await store.grants_to("reader") == {
        "a": frozenset({"semantic", "episodic"}),
        "b": None,
    }


async def test_revoked_grants_convey_nothing(store: SharedMemory) -> None:
    await store.grant("a", "reader")
    await store.revoke("a", "reader")
    assert await store.grants_to("reader") == {}


async def test_grants_from_lists_a_grantors_live_grants(store: SharedMemory) -> None:
    await store.grant("a", "b")
    await store.grant("a", "c", memory_types=["episodic"])
    grants = await store.grants_from("a")
    assert {g.grantee for g in grants} == {"b", "c"}
