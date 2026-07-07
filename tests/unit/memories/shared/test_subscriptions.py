"""Subscriptions (ADR-016, v0.1 minimal): standing-query records."""

from __future__ import annotations

from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.shared.subscriptions import (
    SUBSCRIPTION_ATTRIBUTE,
    active_subscriptions,
    make_subscription_record,
    parse_subscription,
)


def test_subscription_record_shape_and_parse_round_trip() -> None:
    record = make_subscription_record("team-a", "deployment incidents last week")
    assert record.memory_type == "shared"
    assert record.attribute == SUBSCRIPTION_ATTRIBUTE
    assert record.content == "deployment incidents last week"
    sub = parse_subscription(record)
    assert sub is not None
    assert sub.namespace == "team-a"
    assert sub.query == "deployment incidents last week"
    assert sub.record_id == record.record_id


def test_parse_rejects_non_subscription_shapes() -> None:
    fact = MemoryRecord(namespace="a", memory_type="semantic", content="x")
    assert parse_subscription(fact) is None
    grantish = make_subscription_record("a", "q").model_copy(update={"attribute": "grant"})
    assert parse_subscription(grantish) is None


def test_active_subscriptions_excludes_archived_quarantined_and_grants() -> None:
    live = make_subscription_record("a", "q1")
    done = make_subscription_record("a", "q2").model_copy(update={"status": RecordStatus.ARCHIVED})
    held = make_subscription_record("a", "q3").model_copy(update={"quarantined": True})
    other = MemoryRecord(namespace="a", memory_type="shared", content="{}", attribute="grant")
    assert [r.record_id for r in active_subscriptions([live, done, held, other])] == [
        live.record_id
    ]
