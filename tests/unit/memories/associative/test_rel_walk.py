"""W12/G27 (ADR-061): ``AssociativeMemory.rel_walk``, the directed typed walk."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from assoc_helpers import EventLog, FakeStorage

from memspine.core.records import MemoryRecord
from memspine.memories.associative.links import link_event
from memspine.memories.associative.store import AssociativeMemory
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph


@pytest.fixture
def memory(storage: FakeStorage, graph: SQLiteAdjacencyGraph, log: EventLog) -> AssociativeMemory:
    return AssociativeMemory(storage, graph, log.append)


async def _chain(
    storage: FakeStorage, log: EventLog, make_record: Callable[..., MemoryRecord]
) -> list[MemoryRecord]:
    """effect a -because-> b -because-> c; a -related-> d; e -because-> a."""
    a, b, c, d, e = (storage.add(make_record(name)) for name in "abcde")
    for src, dst, rel in (
        (a, b, "because"),
        (b, c, "because"),
        (a, d, "related"),
        (e, a, "because"),
    ):
        await log.append(link_event("default", src.record_id, dst.record_id, rel, 0.5, "rule"))
    return [a, b, c, d, e]


async def test_walk_follows_only_the_rels_in_their_direction(
    memory: AssociativeMemory,
    storage: FakeStorage,
    log: EventLog,
    make_record: Callable[..., MemoryRecord],
) -> None:
    a, b, c, _d, _e = await _chain(storage, log, make_record)
    reached = await memory.rel_walk(
        "default", [a.record_id], {"because"}, depth=2, admit=lambda r: True
    )
    assert [(r.record_id, hops, origin) for r, hops, origin in reached] == [
        (b.record_id, 1, a.record_id),
        (c.record_id, 2, a.record_id),
    ]
    one = await memory.rel_walk(
        "default", [a.record_id], {"because"}, depth=1, admit=lambda r: True
    )
    assert [r.record_id for r, _, _ in one] == [b.record_id]


async def test_refused_record_is_a_dead_end_and_namespace_is_kept(
    memory: AssociativeMemory,
    storage: FakeStorage,
    log: EventLog,
    make_record: Callable[..., MemoryRecord],
) -> None:
    a, b, _c, _d, _e = await _chain(storage, log, make_record)
    reached = await memory.rel_walk(
        "default", [a.record_id], {"because"}, depth=2, admit=lambda r: r.record_id != b.record_id
    )
    assert reached == []
    other = await memory.rel_walk(
        "other", [a.record_id], {"because"}, depth=2, admit=lambda r: True
    )
    assert other == []
