"""GP-2 (#13): entity nodes + ``mentions`` edges, derived in the graph projector.

RM-130: entity ids are opaque and minted on the write path (``with_entity_links``); the
projectors read them from the WRITE event's ``entity_links``."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from memspine.exceptions import ConflictError
from memspine.memories.associative.entities import (
    MENTIONS_REL,
    EntityPolicy,
    canonical_entity,
    new_entity_id,
    parse_entity_node,
    record_entity_names,
    surface_node_id,
)
from memspine.memories.associative.entity_index import (
    EntityIndexProjector,
    entity_names,
    with_entity_links,
)
from memspine.memories.associative.links import RESERVED_RELS, link_event
from memspine.memories.associative.projector import GraphProjector
from memspine.memories.associative.store import AssociativeMemory
from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph
from memspine.store.events import EventKind, MemoryEvent
from memspine.store.records import MemoryRecord

ON = EntityPolicy.from_policy(True)


class _Writer:
    """The write door's entity step: links minted into the event, then every projector."""

    def __init__(self, graph: SqlAdjacencyGraph, policy: EntityPolicy | None = ON) -> None:
        self.storage = object()
        self.index = EntityIndexProjector(self.storage)
        self.graph = GraphProjector(graph, policy)
        self.policy = policy
        self.log: list[MemoryEvent] = []

    async def apply(self, event: MemoryEvent) -> MemoryEvent:
        event = await with_entity_links(self.storage, event, self.policy, projector=self.index)
        event = event.model_copy(update={"seq": len(self.log) + 1})
        self.log.append(event)
        await self.index.apply(event)
        await self.graph.apply(event)
        return event

    async def name(self, node: str) -> str:
        ns = node.split(":")[1]
        return (await entity_names(self.storage, ns, [node]))[node]


def _fact(
    make_record: Callable[..., MemoryRecord],
    entity: str | None,
    dst: str | None = None,
    namespace: str = "default",
    **kwargs: object,
) -> MemoryRecord:
    tags = ["kind:event", "rel:read", f"dst:{dst}"] if dst is not None else []
    return make_record(
        f"{entity} -> {dst}", namespace=namespace, entity=entity, tags=tags, **kwargs
    )


def _forget(record: MemoryRecord) -> MemoryEvent:
    return MemoryEvent(
        kind=EventKind.FORGET, namespace=record.namespace, payload={"record_id": record.record_id}
    )


async def _state(graph: SqlAdjacencyGraph) -> tuple[list[tuple[object, ...]], int]:
    edges = sorted((e.src, e.dst, e.rel_type, e.weight) for e in await graph.edge_list())
    return edges, await graph.node_count()


def test_canonical_names_and_the_opaque_node_id() -> None:
    assert canonical_entity("  MELANIE\t Smith ") == "melanie smith"
    full_width = "".join(chr(ord(c) + 0xFEE0) for c in "Melanie")
    assert canonical_entity(full_width) == "melanie"  # NFKC folds full-width
    node = new_entity_id("org/a")
    ns, local = parse_entity_node(node) or ("", "")
    assert ns == "org/a" and len(local) == 36 and local[14] == "7"  # a uuid7
    assert new_entity_id("org/a") != node
    assert parse_entity_node("some-record-id") is None
    # RM-130: a surface node never parses as a canonical entity
    assert parse_entity_node(surface_node_id("org/a", "person", "melanie")) is None
    assert parse_entity_node("ent:org/a:person:melanie") is None


def test_policy_rejects_junk_names_and_honours_the_allowed_list(
    make_record: Callable[..., MemoryRecord],
) -> None:
    assert EntityPolicy.from_policy(False) is None
    assert EntityPolicy.from_policy({}) is None
    assert ON is not None
    for junk in ("luck", "Tomorrow", "it", "she", "x", "42"):
        assert not ON.accepts(canonical_entity(junk))
    assert ON.accepts("melanie")
    allowed = EntityPolicy.from_policy({"allowed": ["Melanie"]})
    assert allowed is not None
    assert allowed.accepts("melanie") and not allowed.accepts("caroline")
    custom = EntityPolicy.from_policy({"blocklist": ["Melanie"]})
    assert custom is not None
    assert not custom.accepts("melanie") and custom.accepts("luck")
    # A self-edge (destination == source) adds no second mention.
    record = _fact(make_record, "Melanie", "MELANIE")
    assert record_entity_names(record, ON) == ["melanie"]
    assert record_entity_names(_fact(make_record, "Melanie", "tomorrow"), ON) == ["melanie"]


async def test_write_projects_entity_nodes_and_trust_weighted_mentions(
    graph: SqlAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    writer = _Writer(graph)
    record = _fact(make_record, "Melanie", "Charlotte's Web", trust=0.6)
    await writer.apply(write_event(record))
    mentions = {
        (await writer.name(e.dst), e.weight)
        for e in await graph.edges_of(record.record_id)
        if e.rel_type == MENTIONS_REL
    }
    assert mentions == {("melanie", 0.6), ("charlotte's web", 0.6)}
    assert await graph.node_count() == 3


async def test_policy_off_projects_no_entities(
    graph: SqlAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    writer = _Writer(graph, None)
    event = await writer.apply(write_event(_fact(make_record, "Melanie", "Dune")))
    assert "entity_links" not in event.payload
    assert await graph.node_count() == 1 and await graph.edge_count() == 0


async def test_forget_cascades_mentions_and_orphaned_entities(
    graph: SqlAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    writer = _Writer(graph)
    first = _fact(make_record, "Melanie", "Dune")
    second = _fact(make_record, "Melanie", "Emma")
    for record in (first, second):
        await writer.apply(write_event(record))
    mentioned = [e.dst for e in await graph.edges_of(first.record_id) if e.rel_type == MENTIONS_REL]
    [dune] = [node for node in mentioned if await writer.name(node) == "dune"]
    await writer.apply(_forget(first))
    nodes = {await writer.name(n.node_id) for n in await graph.neighbors(second.record_id)}
    assert nodes == {"melanie", "emma"}  # one "melanie" entity: a unique-name link
    # "dune" lost its only mention and is gone; "melanie" is still mentioned.
    assert await graph.edges_of(dune) == []
    assert await entity_names(writer.storage, "default", [dune]) == {}
    assert await graph.node_count() == 3
    await writer.apply(_forget(second))
    assert await graph.node_count() == 0 and await graph.edge_count() == 0


async def test_rebuild_equals_incremental(
    graph: SqlAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    writer = _Writer(graph)
    a = _fact(make_record, "Melanie", "Dune")
    b = _fact(make_record, "Caroline", "Denver")
    c = _fact(make_record, "Melanie", "Emma", trust=0.0)  # trust 0: no mention at all
    renamed = a.model_copy(update={"tags": ["kind:event", "rel:read", "dst:Persuasion"]})
    for event in [
        write_event(a),
        write_event(b),
        write_event(c),
        link_event("default", a.record_id, b.record_id, "related", 0.7, "manual"),
        write_event(renamed),  # re-projected with another dst: "dune" goes stale
        _forget(b),
    ]:
        await writer.apply(event)
    incremental = await _state(graph)
    names = {await writer.name(e[1]) for e in incremental[0] if e[3] > 0 and e[2] == MENTIONS_REL}
    assert names == {"melanie", "persuasion"}
    await writer.graph.reset()
    for event in writer.log:  # replay never mints: the logged links are reused
        await writer.graph.apply(event)
    assert await _state(graph) == incremental


async def test_namespaces_get_their_own_entity_nodes(
    graph: SqlAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    writer = _Writer(graph)
    in_a = _fact(make_record, "Melanie", None, namespace="a")
    in_b = _fact(make_record, "Melanie", None, namespace="b")
    for record in (in_a, in_b):
        await writer.apply(write_event(record))
    [node_a] = {e.dst for e in await graph.edge_list("a")}
    [node_b] = {e.dst for e in await graph.edge_list("b")}
    assert node_a.startswith("ent:a:") and node_b.startswith("ent:b:")
    await writer.apply(_forget(in_a))
    assert [e.dst for e in await graph.edge_list("b")] == [node_b]
    assert await graph.edges_of(node_a) == []


async def test_mentions_is_a_reserved_rel(
    graph: SqlAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
) -> None:
    from assoc_helpers import FakeStorage

    assert MENTIONS_REL in RESERVED_RELS
    storage = FakeStorage()
    a, b = storage.add(make_record("alpha")), storage.add(make_record("beta"))

    async def append(_event: MemoryEvent) -> None:
        return None

    memory = AssociativeMemory(storage, graph, append)
    with pytest.raises(ConflictError, match="reserved"):
        await memory.link("default", a.record_id, b.record_id, rel=MENTIONS_REL)
