"""Shared append identity shape contract for both native records stores."""

from __future__ import annotations

import pytest

from memspine.store.events import EventKind, MemoryEvent
from memspine.store.record_event_ids import validate_record_event_shape


@pytest.mark.parametrize(
    ("kind", "payload"),
    [
        (EventKind.WRITE, {"record": {"record_id": "r", "namespace": "foreign"}}),
        (EventKind.FORGET, {"hard": False}),
        (EventKind.DECAY_TRANSITION, {"record_id": ""}),
        (EventKind.RETRIEVE, {"record_ids": "r"}),
        (EventKind.SESSION, {"record_ids": ["r", 9]}),
        (
            EventKind.MARKER,
            {"marker": "erasure_manifest", "manifest": {"ids": {"record_ids": "r"}}},
        ),
        (EventKind.MARKER, {"marker": "erasure_release", "record_ids": "r"}),
    ],
)
def test_rejects_malformed_record_mutation_ids(kind: EventKind, payload: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        validate_record_event_shape(MemoryEvent(kind=kind, namespace="a", payload=payload))


def test_audit_feedback_and_unrelated_markers_remain_accepted() -> None:
    for event in (
        MemoryEvent(kind=EventKind.FEEDBACK, namespace="a", actor="user", payload={}),
        MemoryEvent(kind=EventKind.MARKER, namespace="a", payload={"marker": "stage_done"}),
        MemoryEvent(kind=EventKind.MARKER, namespace="a", payload={"marker": "erasure_manifest"}),
        MemoryEvent(kind=EventKind.MARKER, namespace="a", payload={"marker": "erasure_release"}),
    ):
        validate_record_event_shape(event)


def test_internal_outcome_receipt_requires_identity() -> None:
    with pytest.raises(ValueError):
        validate_record_event_shape(
            MemoryEvent(
                kind=EventKind.FEEDBACK,
                namespace="a",
                actor="system",
                payload={"feedback": {"channel": "outcome_receipt"}},
            )
        )
