"""RM-130 (ADR-089): per-namespace freshness, bounded catch-up and the resolution pass.

A deferred or raced mention never mints a permanent separate entity: the namespace is caught up
before resolving, and a ``deferred`` / ``new`` entity that has a unique older match is folded
into it by an ``entity_index.link`` event. Two "processes" share one log and one index here.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

import pytest

from memspine.memories.associative import entity_index as module
from memspine.memories.associative.entities import EntityPolicy
from memspine.memories.associative.entity_cache import entity_lookup
from memspine.memories.associative.entity_index import (
    LINK_MARKER,
    LINKS_KEY,
    EntityIndexProjector,
    entity_status,
    relink_events,
    with_entity_links,
)
from memspine.services.storage.entity_index_tables import entity_index_store_of
from memspine.services.storage.entity_projection_memory import MemoryEntityIndexProjection
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

ON = EntityPolicy.from_policy(True)
NS = "default"


class _SharedLog(MemoryEntityIndexProjection):
    """One event log + one shared name index (keyed by this object), for several processes."""

    def __init__(self) -> None:
        super().__init__()

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        return [e for e in self.events if (e.seq or 0) > after_seq][:limit]

    async def read_scoped_events(
        self, namespace: str, kinds: Sequence[EventKind], *, after_seq: int, limit: int
    ) -> tuple[list[MemoryEvent], int]:
        head = len(self.events)
        return [
            e
            for e in self.events
            if after_seq < (e.seq or 0) <= head and e.namespace == namespace and e.kind in kinds
        ][:limit], head

    async def get_offset(self, name: str) -> int:
        return self.marks.get(name, 0)

    async def set_offset(self, name: str, seq: int) -> None:
        self.marks[name] = max(self.marks.get(name, 0), seq)

    def append(self, event: MemoryEvent) -> MemoryEvent:
        return super().append(event)


class _Process:
    """The engine's write door for one process: relink, resolve, append, project, re-check."""

    def __init__(self, log: _SharedLog) -> None:
        self.log = log
        self.index = EntityIndexProjector(log)

    async def resolve(self, record: MemoryRecord) -> MemoryEvent:
        event = MemoryEvent(
            kind=EventKind.WRITE,
            namespace=record.namespace,
            payload={"record": record.model_dump(mode="json")},
        )
        for marker in await relink_events(self.log, event, ON, projector=self.index):
            await self.commit(marker)
        return await with_entity_links(self.log, event, ON, projector=self.index)

    async def commit(self, event: MemoryEvent) -> MemoryEvent:
        appended = self.log.append(event)
        await self.index.apply(appended)
        if any(lk.get("new") for lk in appended.payload.get(LINKS_KEY, ())):
            for marker in await relink_events(self.log, appended, ON, projector=self.index):
                await self.commit(marker)
        return appended

    async def write(self, record: MemoryRecord) -> dict[str, Any]:
        event = await self.commit(await self.resolve(record))
        return dict(event.payload[LINKS_KEY][0])


def _rec(make_record: Callable[..., MemoryRecord], name: str, *tags: str) -> MemoryRecord:
    return make_record(f"about {name}", entity=name, tags=list(tags))


def _links_markers(log: _SharedLog) -> list[MemoryEvent]:
    return [e for e in log.events if e.payload.get("marker") == LINK_MARKER]


async def test_other_namespaces_backlog_never_defers(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _SharedLog()
    a = _Process(log)
    first = await a.write(_rec(make_record, "Alice"))
    for i in range(module._CATCH_UP_PAGE * module._CATCH_UP_PAGES + 10):
        log.append(  # unprojected events of ANOTHER namespace
            MemoryEvent(kind=EventKind.FORGET, namespace="other", payload={"record_id": f"x{i}"})
        )
    second = await a.write(_rec(make_record, "Alice"))
    assert second["basis"] == "unique_name" and second["entity_id"] == first["entity_id"]


async def test_direct_resolution_ignores_other_namespace_only_backlog(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _SharedLog()
    process = _Process(log)
    first = await process.write(_rec(make_record, "Alice"))
    for i in range(module._CATCH_UP_PAGE * module._CATCH_UP_PAGES + 1):
        log.append(
            MemoryEvent(kind=EventKind.FORGET, namespace="other", payload={"record_id": f"x{i}"})
        )
    record = _rec(make_record, "Alice")
    event = MemoryEvent(
        kind=EventKind.WRITE,
        namespace=record.namespace,
        payload={"record": record.model_dump(mode="json")},
    )
    resolved = await with_entity_links(log, event, ON, projector=process.index)
    [link] = resolved.payload[LINKS_KEY]
    assert link["basis"] == "unique_name" and link["entity_id"] == first["entity_id"]


async def test_scoped_catchup_exact_boundary_and_over_budget(
    make_record: Callable[..., MemoryRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_CATCH_UP_PAGE", 2)
    monkeypatch.setattr(module, "_CATCH_UP_PAGES", 1)
    log = _SharedLog()
    process = _Process(log)
    first = await process.write(_rec(make_record, "Alice"))
    for i in range(40):
        log.append(
            MemoryEvent(kind=EventKind.FORGET, namespace="other", payload={"record_id": str(i)})
        )
    calls: list[tuple[int, int]] = []
    original = log.read_scoped_events

    async def counted(
        namespace: str, kinds: Sequence[EventKind], *, after_seq: int, limit: int
    ) -> tuple[list[MemoryEvent], int]:
        events, head = await original(namespace, kinds, after_seq=after_seq, limit=limit)
        calls.append((limit, len(events)))
        return events, head

    monkeypatch.setattr(log, "read_scoped_events", counted)
    for i in range(2):  # exactly the budget: complete
        log.append(MemoryEvent(kind=EventKind.FORGET, namespace=NS, payload={"record_id": f"a{i}"}))
    record = _rec(make_record, "Alice")
    event = MemoryEvent(
        kind=EventKind.WRITE, namespace=NS, payload={"record": record.model_dump(mode="json")}
    )
    exact = await with_entity_links(log, event, ON, projector=process.index)
    assert exact.payload[LINKS_KEY][0]["basis"] == "unique_name"
    assert exact.payload[LINKS_KEY][0]["entity_id"] == first["entity_id"]
    assert calls[-1] == (3, 2)  # one bounded query, foreign events did not consume the budget
    for i in range(3):  # budget + 1: first check must defer and advance only two events
        log.append(MemoryEvent(kind=EventKind.FORGET, namespace=NS, payload={"record_id": f"b{i}"}))
    prior_mark = process.index.caught_up[NS]
    deferred = await with_entity_links(log, event, ON, projector=process.index)
    assert deferred.payload[LINKS_KEY][0]["basis"] == "deferred"
    assert calls[-1] == (3, 3)
    assert process.index.caught_up[NS] == prior_mark + 2
    completed = await with_entity_links(log, event, ON, projector=process.index)
    assert completed.payload[LINKS_KEY][0]["basis"] == "unique_name"


async def test_scoped_head_cannot_include_event_committed_after_head_read(
    make_record: Callable[..., MemoryRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _SharedLog()
    process = _Process(log)
    await process.write(_rec(make_record, "Alice"))
    old_head = len(log.events)
    original = log.read_scoped_events
    inserted = False

    async def append_after_head(
        namespace: str, kinds: Sequence[EventKind], *, after_seq: int, limit: int
    ) -> tuple[list[MemoryEvent], int]:
        nonlocal inserted
        events, head = await original(namespace, kinds, after_seq=after_seq, limit=limit)
        if not inserted:
            log.append(
                MemoryEvent(kind=EventKind.FORGET, namespace=NS, payload={"record_id": "later"})
            )
            inserted = True
        return events, head

    monkeypatch.setattr(log, "read_scoped_events", append_after_head)
    assert await module.index_watermark(log, NS, projector=process.index) == old_head
    assert process.index.caught_up[NS] == old_head
    assert await module.index_watermark(log, NS, projector=process.index) == old_head + 1


async def test_no_projector_requires_no_pending_namespace_index_events() -> None:
    log = _SharedLog()
    log.append(MemoryEvent(kind=EventKind.FORGET, namespace="other", payload={"record_id": "x"}))
    assert await module._namespace_mark(log, NS, None) == 1
    log.append(MemoryEvent(kind=EventKind.FORGET, namespace=NS, payload={"record_id": "y"}))
    assert await module._namespace_mark(log, NS, None) is None


async def test_another_processs_unprojected_write_is_caught_up(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _SharedLog()
    a, b = _Process(log), _Process(log)
    # B appended "Alice" but has not projected it into the shared index yet
    b_event = log.append(await b.resolve(_rec(make_record, "Alice")))
    b_id = b_event.payload[LINKS_KEY][0]["entity_id"]
    linked = await a.write(_rec(make_record, "Alice"))
    assert linked["basis"] == "unique_name" and linked["entity_id"] == b_id
    assert not _links_markers(log)


async def test_a_capped_backlog_defers_then_the_resolution_pass_links_it(
    make_record: Callable[..., MemoryRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module, "_CATCH_UP_PAGE", 2)
    monkeypatch.setattr(module, "_CATCH_UP_PAGES", 1)
    log = _SharedLog()
    a, b = _Process(log), _Process(log)
    b_event = log.append(await b.resolve(_rec(make_record, "Alice")))
    b_id = b_event.payload[LINKS_KEY][0]["entity_id"]
    for i in range(4):  # more of B's unprojected namespace events than one capped check reads
        log.append(MemoryEvent(kind=EventKind.FORGET, namespace=NS, payload={"record_id": f"x{i}"}))
    deferred = await a.write(_rec(make_record, "Alice"))
    assert deferred["basis"] == "deferred" and deferred["entity_id"] != b_id
    # later writes catch the namespace up; the pass folds the deferred entity into B's
    for _ in range(3):
        await a.write(_rec(make_record, "Bob"))
    again = await a.write(_rec(make_record, "Alice"))
    assert again["basis"] == "unique_name" and again["entity_id"] == b_id  # never ambiguous
    [marker] = _links_markers(log)
    assert marker.payload["entity_id"] == deferred["entity_id"]
    assert marker.payload["target"] == b_id
    lookup = await entity_lookup(log, NS)
    assert set(lookup.expand([b_id])) == {b_id, deferred["entity_id"]}
    # a resolution link is no identity evidence: both stay provisional
    assert await entity_status(log, NS, deferred["entity_id"]) == "provisional"
    assert await entity_status(log, NS, b_id) == "provisional"


async def test_a_cross_process_race_folds_the_newer_entity_into_the_older(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _SharedLog()
    a, b = _Process(log), _Process(log)
    # both resolve "Alice" before either appends: both mint a new entity
    a_event = await a.resolve(_rec(make_record, "Alice"))
    b_event = await b.resolve(_rec(make_record, "Alice"))
    assert a_event.payload[LINKS_KEY][0]["basis"] == "new"
    assert b_event.payload[LINKS_KEY][0]["basis"] == "new"
    older = (await a.commit(a_event)).payload[LINKS_KEY][0]["entity_id"]
    newer = (await b.commit(b_event)).payload[LINKS_KEY][0]["entity_id"]
    [marker] = _links_markers(log)  # B's re-check after its append saw both
    assert (marker.payload["entity_id"], marker.payload["target"]) == (newer, older)
    third = await a.write(_rec(make_record, "Alice"))
    assert third["basis"] == "unique_name" and third["entity_id"] == older
    # replay reproduces the fold (the link is an event, never re-derived)
    before = await _rows(log)
    replayed = EntityIndexProjector(log)
    await replayed.reset()
    for event in log.events:
        await replayed.apply(event)
    assert await _rows(log) == before


async def test_the_pass_never_folds_an_explicit_or_type_clashing_entity(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _SharedLog()
    a = _Process(log)
    await a.write(_rec(make_record, "Alex", "entity_type:person"))
    explicit = await a.write(_rec(make_record, "Alex", "entity_type:person", "entity_ref:new"))
    clash = await a.write(_rec(make_record, "Alex", "entity_type:location"))
    assert explicit["basis"] == "explicit" and clash["basis"] == "type_clash"
    await a.write(_rec(make_record, "Alex", "entity_type:location"))
    assert not _links_markers(log)


async def _rows(log: _SharedLog) -> list[tuple[object, ...]]:
    store = entity_index_store_of(log)
    out: list[tuple[object, ...]] = []
    for kind in ("entity", "name", "link", "merge", "cite"):
        out.extend(
            (r.kind, r.key, r.entity_id, r.ref, sorted(r.data.items()))
            for r in await store.select(NS, kind)
        )
    return out
