"""RM-130 (ADR-089): opaque entity ids, the auto-link rule, evidence-cited merges and replay.

Model-free. The write door's entity step (``with_entity_links``) mints ids into the event; the
``entity_index`` projector folds them into the name index; replay of the log reproduces both.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from memspine.memories.associative.entities import EntityPolicy
from memspine.memories.associative.entity_index import (
    LINKS_KEY,
    EntityIndexProjector,
    alias_event,
    entity_status,
    merge_event,
    with_entity_links,
)
from memspine.services.storage.entity_index_tables import entity_index_store_of
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

ON = EntityPolicy.from_policy(True)
NS = "default"


class _Log:
    def __init__(self) -> None:
        self.storage = object()
        self.index = EntityIndexProjector(self.storage)
        self.events: list[MemoryEvent] = []

    async def append(self, event: MemoryEvent) -> MemoryEvent:
        event = await with_entity_links(self.storage, event, ON, projector=self.index)
        event = event.model_copy(update={"seq": len(self.events) + 1})
        self.events.append(event)
        await self.index.apply(event)
        return event

    async def write(self, record: MemoryRecord) -> list[dict[str, Any]]:
        event = await self.append(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=record.namespace,
                payload={"record": record.model_dump(mode="json")},
            )
        )
        return list(event.payload.get(LINKS_KEY, []))

    async def subject(self, record: MemoryRecord) -> dict[str, Any]:
        return (await self.write(record))[0]

    async def forget(self, record_id: str) -> None:
        await self.append(
            MemoryEvent(kind=EventKind.FORGET, namespace=NS, payload={"record_id": record_id})
        )

    async def rows(self) -> list[tuple[object, ...]]:
        store = entity_index_store_of(self.storage)
        out: list[tuple[object, ...]] = []
        for kind in ("entity", "name", "link", "merge", "cite"):
            out.extend(
                (r.kind, r.key, r.entity_id, r.ref, r.match, sorted(r.data.items()))
                for r in await store.select(NS, kind)
            )
        return out


def _rec(
    make_record: Callable[..., MemoryRecord], name: str, *tags: str, **kw: Any
) -> MemoryRecord:
    content = str(kw.pop("content", f"about {name}"))
    return make_record(content, entity=name, tags=list(tags), **kw)


async def test_unique_name_links_but_stays_provisional(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    first = await log.subject(_rec(make_record, "Alex"))
    assert first["new"] and first["basis"] == "new"
    second = await log.subject(_rec(make_record, "ALEX "))
    assert second["entity_id"] == first["entity_id"]
    assert second["basis"] == "unique_name" and second["status"] == "provisional"
    assert await entity_status(log.storage, NS, first["entity_id"]) == "provisional"


async def test_two_same_named_people_stay_distinct_and_ambiguity_goes_provisional(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    one = await log.subject(_rec(make_record, "Alex", "entity_type:person"))
    two = await log.subject(_rec(make_record, "Alex", "entity_type:person", "entity_ref:new"))
    assert two["new"] and two["entity_id"] != one["entity_id"]
    assert two["basis"] == "explicit" and two["status"] == "confirmed"
    # a pinned mention goes to the person it names, never the other Alex
    pinned = await log.subject(_rec(make_record, "Alex", f"entity_ref:{one['entity_id']}"))
    assert pinned["entity_id"] == one["entity_id"] and pinned["basis"] == "explicit"
    # a bare "Alex" now has two candidates: a provisional placeholder, candidates recorded
    ambiguous = await log.subject(_rec(make_record, "Alex"))
    assert ambiguous["new"] and ambiguous["basis"] == "ambiguous"
    assert ambiguous["entity_id"] not in (one["entity_id"], two["entity_id"])
    assert ambiguous["candidates"] == sorted([one["entity_id"], two["entity_id"]])
    # the next ambiguous mention joins that placeholder instead of minting another
    again = await log.subject(_rec(make_record, "Alex"))
    assert again["entity_id"] == ambiguous["entity_id"] and not again["new"]
    assert again["basis"] == "ambiguous"


async def test_type_clash_gives_a_new_provisional_entity(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    person = await log.subject(_rec(make_record, "Jordan", "entity_type:person"))
    place = await log.subject(_rec(make_record, "Jordan", "entity_type:location"))
    assert place["new"] and place["basis"] == "type_clash"
    assert place["candidates"] == [person["entity_id"]]
    assert await entity_status(log.storage, NS, place["entity_id"]) == "provisional"
    # untyped is compatible with both: two candidates, so no auto-link
    untyped = await log.subject(_rec(make_record, "Jordan"))
    assert untyped["basis"] == "ambiguous"
    # a typed mention links to the one compatible entity
    again = await log.subject(_rec(make_record, "Jordan", "entity_type:location"))
    assert again["entity_id"] == place["entity_id"] and again["basis"] == "unique_name"
    unknown = await log.subject(_rec(make_record, "Jordan", "entity_type:unknown"))
    assert unknown["basis"] == "ambiguous"  # "unknown" counts as untyped


async def test_deferred_index_never_auto_links(make_record: Callable[..., MemoryRecord]) -> None:
    log = _Log()
    first = await log.subject(_rec(make_record, "Pia"))

    class _Behind:  # the log has an event the index has not applied
        async def get_offset(self, name: str) -> int:
            return 0

        async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
            return [MemoryEvent(kind=EventKind.WRITE, namespace=NS, seq=after_seq + 1)]

    from memspine.memories.associative import entity_index as module

    record = _rec(make_record, "Pia")
    links = await module.resolve_record_links(_Behind(), record, ON)
    assert links[0]["new"] and links[0]["basis"] == "deferred"
    assert links[0]["entity_id"] != first["entity_id"]


async def test_merge_with_evidence_then_undo_on_erasure(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    mel = await log.subject(_rec(make_record, "Mel"))
    melanie = await log.subject(_rec(make_record, "Melanie"))
    evidence = _rec(make_record, "Melanie", content="Mel is short for Melanie")
    await log.write(evidence)
    await log.append(merge_event(NS, melanie["entity_id"], mel["entity_id"], [evidence.record_id]))
    assert await entity_status(log.storage, NS, mel["entity_id"]) == "confirmed"
    store = entity_index_store_of(log.storage)
    assert await store.select(NS, "merge", entity_id=melanie["entity_id"])
    # a merge without evidence is never applied
    await log.append(merge_event(NS, mel["entity_id"], melanie["entity_id"], []))
    assert len(await store.select(NS, "merge")) == 1
    await log.forget(evidence.record_id)
    assert await store.select(NS, "merge") == []
    assert await entity_status(log.storage, NS, mel["entity_id"]) == "contested"


async def test_alias_with_evidence_links_and_undoes(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    melanie = await log.subject(_rec(make_record, "Melanie"))
    evidence = _rec(make_record, "Melanie", content="everyone calls Melanie Mel")
    await log.write(evidence)
    await log.append(alias_event(NS, melanie["entity_id"], "Mel", [evidence.record_id]))
    linked = await log.subject(_rec(make_record, "Mel"))
    assert linked["entity_id"] == melanie["entity_id"] and linked["basis"] == "unique_name"
    assert await entity_status(log.storage, NS, melanie["entity_id"]) == "confirmed"
    await log.forget(evidence.record_id)
    assert await entity_status(log.storage, NS, melanie["entity_id"]) == "contested"


async def test_rebuild_from_the_log_reproduces_ids_and_index(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    a = _rec(make_record, "Alex", "entity_type:person", "dst:Rome", "rel:lives_in")
    await log.write(a)
    await log.write(_rec(make_record, "Alex", "entity_type:person", "entity_ref:new"))
    await log.write(_rec(make_record, "Alex"))
    evidence = _rec(make_record, "Rome")
    await log.write(evidence)
    links = [lk for e in log.events for lk in e.payload.get(LINKS_KEY, [])]
    await log.append(
        merge_event(NS, links[0]["entity_id"], links[3]["entity_id"], [evidence.record_id])
    )
    await log.forget(a.record_id)
    before = await log.rows()
    ids_before = [lk["entity_id"] for lk in links]
    await log.index.reset()
    assert await log.rows() == []
    for event in log.events:  # replay: links are read from the log, never minted
        await log.index.apply(event)
    assert await log.rows() == before
    assert [
        lk["entity_id"] for e in log.events for lk in e.payload.get(LINKS_KEY, [])
    ] == ids_before
