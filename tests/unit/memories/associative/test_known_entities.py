"""DS-11 / DS-12 (ADR-090 item 6): KNOWN entities read from the RM-130 entity index carry
source, version, known-time and visibility; only the caller's entities are included, and a
revoked or erased alias is gone. Model-free: the index projector over an in-process store."""

from __future__ import annotations

from collections.abc import Callable, Sequence

import pytest

from memspine.derive.prompt_context import (
    KnownEntity,
    KnownSet,
    KnownVisibility,
    PromptContextBuilder,
)
from memspine.memories.associative import entity_index as entity_index_module
from memspine.memories.associative.entities import EntityPolicy
from memspine.memories.associative.entity_index import (
    INDEX_PROJECTOR,
    LINKS_KEY,
    EntityIndexProjector,
    alias_event,
    with_entity_links,
)
from memspine.memories.associative.known_entities import (
    KNOWN_SOURCE,
    KnownEntityCursor,
    index_known_entities,
)
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

ON = EntityPolicy.from_policy(True)


class _Events:
    """The event log a cursor's watermark is proven against (one process, in-order applies)."""

    def __init__(self, log: _Log) -> None:
        self._log = log

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        return [e for e in self._log.events if (e.seq or 0) > after_seq][:limit]

    async def get_offset(self, name: str) -> int:
        return 0


class _Log:
    def __init__(self) -> None:
        self.storage = _Events(self)
        self.index = EntityIndexProjector(self.storage)
        self.events: list[MemoryEvent] = []

    def cursor(self, **bounds: int) -> KnownEntityCursor:
        return KnownEntityCursor(lambda: self.storage, projector=lambda: self.index, **bounds)

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


# --- DS-12: the bounded, incremental refresh -----------------------------------------------------


async def _people(log: _Log, make_record: Callable[..., MemoryRecord], *names: str) -> list[str]:
    return [(await log.write(_rec(make_record, name)))[0] for name in names]


async def test_refresh_is_incremental_past_its_cursor(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    await _people(log, make_record, "Alex", "Brin", "Cato")
    cursor = log.cursor()
    first = await cursor("a", "")
    assert cursor.rows_read == 3 and not first.deferred
    assert {k.name for k in first} == {"Alex", "Brin", "Cato"}
    await cursor("a", "")
    assert cursor.rows_read == 0  # nothing new: no walk of the index
    await _people(log, make_record, "Dara")
    again = await cursor("a", "")
    assert cursor.rows_read == 1 and "Dara" in {k.name for k in again}


async def test_a_partly_read_set_is_deferred_until_the_cursor_catches_up(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    await _people(log, make_record, "Alex", "Brin", "Cato", "Dara", "Emil")
    cursor = log.cursor(page=2, max_pages=1)
    first = await cursor("a", "")
    assert cursor.rows_read == 2
    assert first.deferred and first.reason == "behind" and not first.supports_auto_link
    second = await cursor("a", "")
    assert second.deferred and cursor.rows_read == 2
    third = await cursor("a", "")
    assert not third.deferred and third.supports_auto_link
    assert {k.name for k in third} == {"Alex", "Brin", "Cato", "Dara", "Emil"}


async def test_a_capped_set_is_deferred_and_mentioned_names_come_first(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    await _people(log, make_record, "Alex", "Brin", "Cato")
    capped = await log.cursor(max_entities=2)("a", "")
    assert capped.deferred and capped.reason == "capped" and len(capped) == 2
    assert not capped.supports_auto_link  # ADR-089: a capped set never supports auto-link
    one = await log.cursor(limit=1)("a", "[1] I saw alex")
    assert [k.name for k in one] == ["Alex"]


async def test_an_alias_erased_after_it_was_read_is_dropped(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    entity_id, _ = await log.write(_rec(make_record, "Alex"))
    evidence = _rec(make_record, "Alex")
    await log.write(evidence)
    await log.alias("a", entity_id, "Lexi", evidence.record_id)
    cursor = log.cursor()
    [known] = await cursor("a", "")
    assert known.aliases == ("Lexi",)
    await log.forget("a", evidence.record_id, hard=True)
    [known] = await cursor("a", "")
    assert known.aliases == ()


async def test_the_builder_reports_a_deferred_source() -> None:
    async def store(_ns: str, _text: str) -> Sequence[KnownEntity]:
        return KnownSet([KnownEntity("Alex")], deferred=True, reason="capped")

    built = await PromptContextBuilder(store).build("a", "x", [])
    assert built.deferred == ("store",) and not built.known_complete
    full = await PromptContextBuilder(None).build("a", "x", [])
    assert full.known_complete


async def test_sql_store_pages_in_keyset_order() -> None:
    from sqlalchemy.ext.asyncio import create_async_engine

    from memspine.services.storage.entity_index_tables import (
        EntityIndexRow,
        SqlEntityIndexStore,
        entity_index_metadata,
        row_cursor,
    )

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(entity_index_metadata.create_all)
    store = SqlEntityIndexStore(engine)
    rows = [EntityIndexRow("a", "name", f"n{i}", f"e{i % 3}", "name", seq=i // 2) for i in range(7)]
    await store.put([*rows, EntityIndexRow("b", "name", "other", "x", "name", seq=0)])
    seen: list[str] = []
    after = None
    while True:
        page = await store.select_after("a", "name", after=after, limit=3)
        seen.extend(r.key for r in page)
        if len(page) < 3:
            break
        after = row_cursor(page[-1])
    assert seen == [r.key for r in sorted(rows, key=row_cursor)]
    await engine.dispose()


# --- RM-154: provable completeness, removal eviction, deterministic selection -------------------

#: The removed DS-12 lookback (sequence numbers) the late row below lands beyond.
_OLD_LOOKBACK = 64


class _SharedLog:
    """One event log + one shared name index (keyed by this object), for several processes."""

    def __init__(self) -> None:
        self.events: list[MemoryEvent] = []
        self.marks: dict[str, int] = {}

    async def read_events(self, after_seq: int = 0, limit: int = 1000) -> list[MemoryEvent]:
        return [e for e in self.events if (e.seq or 0) > after_seq][:limit]

    async def get_offset(self, name: str) -> int:
        return self.marks.get(name, 0)

    async def set_offset(self, name: str, seq: int) -> None:
        self.marks[name] = max(self.marks.get(name, 0), seq)

    def append(self, event: MemoryEvent) -> MemoryEvent:
        appended = event.model_copy(update={"seq": len(self.events) + 1})
        self.events.append(appended)
        return appended


class _Process:
    """One process over the shared log: its own projector; entity ids minted once into events."""

    def __init__(self, log: _SharedLog) -> None:
        self.log = log
        self.index = EntityIndexProjector(log)

    def cursor(self, **bounds: int) -> KnownEntityCursor:
        return KnownEntityCursor(lambda: self.log, projector=lambda: self.index, **bounds)

    async def mint(self, record: MemoryRecord) -> MemoryEvent:
        # No projector: this write path does not catch the namespace up (ids are still minted).
        return await with_entity_links(
            self.log,
            MemoryEvent(
                kind=EventKind.WRITE,
                namespace=record.namespace,
                payload={"record": record.model_dump(mode="json")},
            ),
            ON,
        )

    async def commit(self, event: MemoryEvent) -> MemoryEvent:
        appended = self.log.append(event)
        await self.index.apply(appended)
        await self.log.set_offset(INDEX_PROJECTOR, int(appended.seq or 0))
        return appended

    async def forget(self, ns: str, record_id: str) -> None:
        await self.commit(
            MemoryEvent(
                kind=EventKind.FORGET, namespace=ns, payload={"record_id": record_id, "hard": True}
            )
        )


async def _names(cursor: KnownEntityCursor) -> set[str]:
    """Every name the cursor's index holds for ``a`` (an unbounded reader of the same state)."""
    wide = KnownEntityCursor(
        cursor._storage, projector=cursor._projector, limit=10_000, max_entities=10_000
    )
    return {k.name for k in await wide("a", "")}


async def test_a_row_projected_late_far_below_the_cursor_is_read_before_complete(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _SharedLog()
    a, b = _Process(log), _Process(log)
    cursor = a.cursor(limit=1000)
    await a.commit(await a.mint(_rec(make_record, "Alex")))
    assert not (await cursor("a", "")).deferred
    # B appends "Zed" and has not projected it; A projects 80 more namespace events above it.
    late = log.append(await b.mint(_rec(make_record, "Zed")))
    for i in range(80):
        await a.commit(await a.mint(_rec(make_record, f"Person{i}")))
    assert late.seq is not None
    assert (log.events[-1].seq or 0) - late.seq > _OLD_LOOKBACK
    known = await cursor("a", "")
    # complete only once every row up to the namespace watermark was read: Zed included
    assert not known.deferred and "Zed" in {k.name for k in known}
    await b.index.apply(late)  # B's own late projection changes nothing
    again = await cursor("a", "")
    assert not again.deferred and "Zed" in {k.name for k in again}
    assert "Zed" in await _names(cursor)


async def test_an_unprovable_watermark_is_deferred_until_the_backlog_is_caught_up(
    make_record: Callable[..., MemoryRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    log = _SharedLog()
    a, b = _Process(log), _Process(log)
    cursor = a.cursor(limit=1000)
    await a.commit(await a.mint(_rec(make_record, "Alex")))
    await cursor("a", "")
    log.append(await b.mint(_rec(make_record, "Zed")))  # projected by no one yet
    for i in range(30):
        log.append(await b.mint(_rec(make_record, f"P{i}")))
    monkeypatch.setattr(entity_index_module, "_CATCH_UP_PAGE", 8)
    monkeypatch.setattr(entity_index_module, "_CATCH_UP_PAGES", 1)
    deferred = 0
    for _ in range(10):
        known = await cursor("a", "")
        if known.deferred:
            assert known.reason == "behind"
            deferred += 1
            continue
        assert "Zed" in {k.name for k in known}  # never complete without the late row
        break
    else:
        raise AssertionError("the cursor never caught up")
    assert deferred >= 1


async def test_an_erasure_evicts_the_names_from_memory_at_once(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    alex = _rec(make_record, "Alex")
    entity_id, _ = await log.write(alex)
    evidence = _rec(make_record, "Alex")
    await log.write(evidence)
    await log.alias("a", entity_id, "Lexi", evidence.record_id)
    await _people(log, make_record, "Brin")
    cursor = log.cursor()
    assert {k.name for k in await cursor("a", "")} == {"Alex", "Brin"}
    await log.forget("a", alex.record_id, hard=True)
    await log.forget("a", evidence.record_id, hard=True)
    # no refresh yet: the cursor's memory holds no copy of the erased entity or its names
    state = cursor._state["a"]
    assert entity_id not in state.entries
    held = {text for entry in state.entries.values() for text, _ in entry.names.values()}
    assert not held & {"alex", "lexi"}
    assert [k.name for k in await cursor("a", "")] == ["Brin"]


async def test_capped_is_recomputed_after_a_removal(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _Log()
    records = [_rec(make_record, name) for name in ("Alex", "Brin", "Cato")]
    for record in records:
        await log.write(record)
    cursor = log.cursor(max_entities=2)
    capped = await cursor("a", "")
    assert capped.reason == "capped" and {k.name for k in capped} == {"Brin", "Cato"}
    await log.forget("a", records[0].record_id, hard=True)  # the entity past the cap goes
    after = await cursor("a", "")
    assert not after.deferred and {k.name for k in after} == {"Brin", "Cato"}
    await log.write(_rec(make_record, "Dara"))
    again = await cursor("a", "")
    assert again.reason == "capped" and {k.name for k in again} == {"Cato", "Dara"}
    await log.forget("a", records[2].record_id, hard=True)  # a kept entity goes: room again
    last = await cursor("a", "")
    assert not last.deferred and {k.name for k in last} == {"Brin", "Dara"}


async def test_another_processs_removal_reaches_the_cursor(
    make_record: Callable[..., MemoryRecord],
) -> None:
    log = _SharedLog()
    a, b = _Process(log), _Process(log)
    brin = _rec(make_record, "Brin")
    await a.commit(await a.mint(_rec(make_record, "Alex")))
    await a.commit(await a.mint(brin))
    cursor = a.cursor()
    assert {k.name for k in await cursor("a", "")} == {"Alex", "Brin"}
    await b.forget("a", brin.record_id)  # B projects the erasure into the shared index
    known = await cursor("a", "")
    assert [k.name for k in known] == ["Alex"] and not known.deferred
    held = {t for e in cursor._state["a"].entries.values() for t, _ in e.names.values()}
    assert "brin" not in held


async def _known_block(cursor: KnownEntityCursor) -> tuple[str, tuple[str | None, ...]]:
    built = await PromptContextBuilder(cursor, cap=3).build("a", "[1] nothing named", [])
    return built.known, tuple(k.entity_id for k in built.manifest)


async def test_a_fresh_process_replay_renders_the_same_known_block(
    make_record: Callable[..., MemoryRecord],
) -> None:
    live = _Log()
    cursor = live.cursor(max_entities=3, limit=3)
    ids: dict[str, str] = {}
    records: dict[str, MemoryRecord] = {}
    for name in ("Alex", "Brin", "Cato", "Dara", "Emil"):
        records[name] = _rec(make_record, name)
        ids[name], _ = await live.write(records[name])
        await _known_block(cursor)  # the live process refreshes as it goes (its own history)
    evidence = _rec(make_record, "Alex")
    await live.write(evidence)
    await live.alias("a", ids["Alex"], "Al", evidence.record_id)
    await _known_block(cursor)
    await live.forget("a", records["Dara"].record_id, hard=True)
    block = await _known_block(cursor)

    fresh = _Log()  # a new process: the index rebuilt by replaying the same log
    for event in live.events:
        fresh.events.append(event)
        await fresh.index.apply(event)
    assert await _known_block(fresh.cursor(max_entities=3, limit=3)) == block
    assert block[1] == (ids["Alex"], ids["Emil"], ids["Cato"])  # rank order, from index state
