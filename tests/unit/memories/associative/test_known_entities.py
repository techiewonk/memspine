"""DS-11 / DS-12 (ADR-090 item 6): KNOWN entities read from the RM-130 entity index carry
source, version, known-time and visibility; only the caller's entities are included, and a
revoked or erased alias is gone. Model-free: the index projector over an in-process store."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from memspine.derive.prompt_context import KnownEntity, KnownVisibility, PromptContextBuilder
from memspine.memories.associative.entities import EntityPolicy
from memspine.memories.associative.entity_index import (
    LINKS_KEY,
    EntityIndexProjector,
    alias_event,
    with_entity_links,
)
from memspine.memories.associative.known_entities import KNOWN_SOURCE, index_known_entities
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

ON = EntityPolicy.from_policy(True)


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

    async def write(self, record: MemoryRecord) -> tuple[str, int]:
        event = await self.append(
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=record.namespace,
                payload={"record": record.model_dump(mode="json")},
            )
        )
        links = list(event.payload.get(LINKS_KEY, []))
        assert event.seq is not None
        return (str(links[0]["entity_id"]) if links else "", event.seq)

    async def alias(self, ns: str, entity_id: str, alias: str, evidence: str) -> int:
        event = await self.append(alias_event(ns, entity_id, alias, [evidence]))
        assert event.seq is not None
        return event.seq

    async def forget(self, ns: str, record_id: str, *, hard: bool = False) -> None:
        await self.append(
            MemoryEvent(
                kind=EventKind.FORGET,
                namespace=ns,
                payload={"record_id": record_id, "hard": hard},
            )
        )


def _rec(make_record: Callable[..., MemoryRecord], name: str, ns: str = "a") -> MemoryRecord:
    return make_record(f"about {name}", namespace=ns, entity=name)


async def test_known_entities_carry_source_version_known_time_and_visibility(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    alex = _rec(make_record, "Alex")
    entity_id, first_seq = await log.write(alex)
    evidence = _rec(make_record, "Alex")
    await log.write(evidence)
    alias_seq = await log.alias("a", entity_id, "Al", evidence.record_id)

    [known] = await index_known_entities(log.storage, "a")
    assert (known.name, known.aliases) == ("Alex", ("Al",))
    assert known.source == KNOWN_SOURCE and known.entity_id == entity_id
    assert known.known_seq == first_seq  # when the index came to know the name
    assert known.version == alias_seq  # its latest name or alias row
    assert known.visibility == KnownVisibility(namespace="a")


async def test_only_the_callers_namespace_is_known(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    await log.write(_rec(make_record, "Alex", "a"))
    await log.write(_rec(make_record, "Brin", "b"))
    assert [k.name for k in await index_known_entities(log.storage, "a")] == ["Alex"]
    assert [k.name for k in await index_known_entities(log.storage, "b")] == ["Brin"]


async def test_a_revoked_or_erased_alias_is_not_known(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    entity_id, _ = await log.write(_rec(make_record, "Alex"))
    soft = _rec(make_record, "Alex")
    hard = _rec(make_record, "Alex")
    await log.write(soft)
    await log.write(hard)
    await log.alias("a", entity_id, "Al", soft.record_id)
    await log.alias("a", entity_id, "Lexi", hard.record_id)
    [known] = await index_known_entities(log.storage, "a")
    assert set(known.aliases) == {"Al", "Lexi"}

    await log.forget("a", soft.record_id)  # the alias's only evidence is revoked
    await log.forget("a", hard.record_id, hard=True)  # an erasure
    [known] = await index_known_entities(log.storage, "a")
    assert known.aliases == ()


async def test_builder_keeps_only_visible_entries_and_reports_their_provenance() -> None:
    async def store(_ns: str, _text: str) -> Sequence[KnownEntity]:
        return [
            KnownEntity(
                "Alex",
                ("Al",),
                source="entity_index",
                entity_id="ent:a:1",
                version=7,
                known_seq=3,
                visibility=KnownVisibility(namespace="a"),
            ),
            KnownEntity("Mallory", source="entity_index", visibility=KnownVisibility("b")),
            KnownEntity(
                "Grace", source="entity_index", visibility=KnownVisibility("b", grantee="a")
            ),
        ]

    built = await PromptContextBuilder(store).build("a", "[1] Alex met Grace", [])
    assert "Mallory" not in built.known
    assert [k.name for k in built.manifest] == ["Alex", "Grace"]
    alex = built.manifest[0]
    assert (alex.entity_id, alex.version, alex.known_seq) == ("ent:a:1", 7, 3)
    assert built.hidden == 1 and built.by_source["hidden"] == 1

    other = await PromptContextBuilder(store).build("b", "x", [])
    assert [k.name for k in other.manifest] == ["Alex", "Mallory", "Grace"][1:]


def test_window_text_hints_have_no_foreign_visibility() -> None:
    hint = KnownEntity("Noor", source="prepass")
    assert hint.visible_to("a") and hint.visible_to("b")
