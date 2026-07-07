"""Event model: ids, fingerprints, canonical serialization (D-37/D-38)."""

from __future__ import annotations

from memspine.core.events import (
    EventKind,
    MemoryEvent,
    canonical_payload,
    fingerprint_payload,
)


def test_fingerprint_is_stable_across_key_order() -> None:
    a = {"x": 1, "y": {"b": 2, "a": 3}}
    b = {"y": {"a": 3, "b": 2}, "x": 1}
    assert fingerprint_payload(a) == fingerprint_payload(b)
    assert canonical_payload(a) == canonical_payload(b)


def test_event_gets_id_and_fingerprint() -> None:
    event = MemoryEvent(kind=EventKind.WRITE, namespace="ns/a", payload={"content": "hi"})
    assert event.event_id
    assert event.seq is None
    assert event.fingerprint == fingerprint_payload({"content": "hi"})


def test_distinct_events_get_distinct_ids() -> None:
    e1 = MemoryEvent(kind=EventKind.WRITE, namespace="ns")
    e2 = MemoryEvent(kind=EventKind.WRITE, namespace="ns")
    assert e1.event_id != e2.event_id
