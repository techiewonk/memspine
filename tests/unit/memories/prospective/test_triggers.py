"""Trigger rules (M13.8/ADR-016): pure, clock-free, deterministic."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.prospective.triggers import (
    due_watches,
    fired_watches,
    invalidation_watches,
)

NOW = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)


def watch(
    *,
    due_at: datetime | None = None,
    entity: str | None = None,
    attribute: str | None = None,
    namespace: str = "ns",
    status: RecordStatus = RecordStatus.ACTIVATED,
    quarantined: bool = False,
    recorded_at: datetime = NOW - timedelta(hours=1),
) -> MemoryRecord:
    return MemoryRecord(
        namespace=namespace,
        memory_type="prospective",
        content="do the thing",
        entity=entity,
        attribute=attribute,
        valid_from=due_at if due_at is not None else recorded_at,
        recorded_at=recorded_at,
        status=status,
        quarantined=quarantined,
    )


def conflict(
    entity: str,
    attribute: str | None,
    *,
    action: str = "updated",
    namespace: str = "ns",
    ts: datetime = NOW - timedelta(minutes=5),
) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.CONFLICT,
        namespace=namespace,
        ts=ts,
        payload={"verdict": "update", "action": action, "fact_key": [entity, attribute]},
    )


# ── due watches ───────────────────────────────────────────────────────────────


def test_due_watch_fires_at_and_after_its_due_time() -> None:
    due = watch(due_at=NOW - timedelta(minutes=1))
    exactly = watch(due_at=NOW)
    future = watch(due_at=NOW + timedelta(minutes=1))
    fired = due_watches([due, exactly, future], NOW)
    assert {record.record_id for record in fired} == {due.record_id, exactly.record_id}


def test_due_watch_stays_pending_until_acknowledged() -> None:
    due = watch(due_at=NOW - timedelta(days=2))
    assert due_watches([due], NOW) == due_watches([due], NOW)  # re-asking loses nothing


def test_quarantined_and_archived_watches_never_fire() -> None:
    held = watch(due_at=NOW - timedelta(hours=1), quarantined=True)
    done = watch(due_at=NOW - timedelta(hours=1), status=RecordStatus.ARCHIVED)
    assert due_watches([held, done], NOW) == []


def test_target_watches_do_not_fire_on_time() -> None:
    target = watch(entity="alice", attribute="city", recorded_at=NOW - timedelta(days=1))
    assert due_watches([target], NOW) == []


def test_non_prospective_records_are_ignored() -> None:
    fact = MemoryRecord(
        namespace="ns", memory_type="semantic", content="x", valid_from=NOW - timedelta(days=1)
    )
    assert due_watches([fact], NOW) == []


# ── invalidation watches ──────────────────────────────────────────────────────


def test_invalidation_fires_on_truth_changing_conflict() -> None:
    target = watch(entity="alice", attribute="city")
    fired = invalidation_watches([target], [conflict("alice", "city", action="updated")])
    assert [record.record_id for record in fired] == [target.record_id]


def test_attribute_none_watches_every_attribute_of_the_entity() -> None:
    target = watch(entity="alice", attribute=None)
    fired = invalidation_watches([target], [conflict("alice", "employer")])
    assert [record.record_id for record in fired] == [target.record_id]


def test_rejected_and_backfill_conflicts_do_not_fire() -> None:
    target = watch(entity="alice", attribute="city")
    events = [
        conflict("alice", "city", action="rejected"),
        conflict("alice", "city", action="added"),  # backfill left the fact standing
    ]
    assert invalidation_watches([target], events) == []


def test_other_entity_attribute_or_namespace_does_not_fire() -> None:
    target = watch(entity="alice", attribute="city")
    events = [
        conflict("bob", "city"),
        conflict("alice", "employer"),
        conflict("alice", "city", namespace="other"),
    ]
    assert invalidation_watches([target], events) == []


def test_conflicts_predating_the_watch_never_fire_it() -> None:
    target = watch(entity="alice", attribute="city", recorded_at=NOW)
    stale = conflict("alice", "city", ts=NOW - timedelta(hours=2))
    assert invalidation_watches([target], [stale]) == []


def test_quarantined_target_watch_never_fires() -> None:
    held = watch(entity="alice", attribute="city", quarantined=True)
    assert invalidation_watches([held], [conflict("alice", "city")]) == []


def test_non_conflict_events_are_ignored() -> None:
    target = watch(entity="alice", attribute="city")
    write = MemoryEvent(kind=EventKind.WRITE, namespace="ns", payload={"record": {}})
    assert invalidation_watches([target], [write]) == []


# ── union ─────────────────────────────────────────────────────────────────────


def test_fired_watches_unions_and_deduplicates_deterministically() -> None:
    early_due = watch(due_at=NOW - timedelta(hours=3))
    target = watch(entity="alice", attribute="city")
    fired = fired_watches([early_due, target], NOW, [conflict("alice", "city")])
    assert [record.record_id for record in fired] == [
        record.record_id
        for record in sorted([early_due, target], key=lambda r: (r.valid_from, r.record_id))
    ]
    # Same inputs, same firing — and no duplicates when both rules match a set.
    assert fired == fired_watches([early_due, target], NOW, [conflict("alice", "city")])
