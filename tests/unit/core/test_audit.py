"""Blast-radius audit (E1): LINK propagation + tombstone evidence (H4, P6)."""

from __future__ import annotations

from memspine.core.audit import trace_taint
from memspine.core.events import EventKind, MemoryEvent


class FakeEventSource:
    def __init__(self, events: list[MemoryEvent]) -> None:
        self._events = [event.model_copy(update={"seq": i + 1}) for i, event in enumerate(events)]

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        return [event for event in self._events if (event.seq or 0) > after_seq][:limit]


def _write(record_id: str) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.WRITE,
        namespace="agent/a",
        actor="tool",
        payload={"record": {"record_id": record_id, "source": {"role": "tool"}}},
    )


def _link(src: str, dst: str, weight: float) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.LINK,
        namespace="agent/a",
        actor="system",
        payload={"src": src, "dst": dst, "rel": "related", "weight": weight, "reason": "t"},
    )


async def test_live_link_propagates_taint_to_the_other_endpoint() -> None:
    source = FakeEventSource([_write("A"), _write("B"), _link("A", "B", 0.9)])
    report = await trace_taint(source, "A")
    assert report.descendants == {"B": "linked@3"}
    assert 3 in report.event_seqs  # the LINK is evidence


async def test_link_taint_propagates_from_either_endpoint() -> None:
    # Undirected: tainting the dst reaches the src too.
    source = FakeEventSource([_write("A"), _write("B"), _link("A", "B", 0.9)])
    report = await trace_taint(source, "B")
    assert report.descendants == {"A": "linked@3"}


async def test_tombstoned_link_is_evidence_but_does_not_propagate() -> None:
    """A weight-0 LINK (prune/supersede tombstone, ADR-015) removed the graph
    reach — it must appear in event_seqs yet never taint the other endpoint."""
    source = FakeEventSource([_write("A"), _write("B"), _link("A", "B", 0.0)])
    report = await trace_taint(source, "A")
    assert report.descendants == {}
    assert 3 in report.event_seqs  # evidence trail keeps the tombstone


async def test_link_taint_is_transitive() -> None:
    source = FakeEventSource(
        [_write("A"), _write("B"), _write("C"), _link("A", "B", 1.0), _link("B", "C", 1.0)]
    )
    report = await trace_taint(source, "A")
    assert set(report.descendants) == {"B", "C"}
    assert report.descendants["C"] == "linked@5"
