"""GP-2 (#13): entity nodes + ``mentions`` edges, derived in the graph projector."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.exceptions import ConflictError
from memspine.memories.associative.entities import (
    MENTIONS_REL,
    EntityPolicy,
    canonical_entity,
    entity_node_id,
    parse_entity_node,
    record_entity_names,
)
from memspine.memories.associative.links import RESERVED_RELS, link_event
from memspine.memories.associative.projector import GraphProjector
from memspine.memories.associative.store import AssociativeMemory
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph

ON = EntityPolicy.from_policy(True)


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


async def _state(graph: SQLiteAdjacencyGraph) -> tuple[list[tuple[object, ...]], int]:
    edges = sorted((e.src, e.dst, e.rel_type, e.weight) for e in await graph.edge_list())
    return edges, await graph.node_count()


def test_canonical_names_and_the_node_id() -> None:
    assert canonical_entity("  MELANIE\t Smith ") == "melanie smith"
    full_width = "".join(chr(ord(c) + 0xFEE0) for c in "Melanie")
    assert canonical_entity(full_width) == "melanie"  # NFKC folds full-width
    node = entity_node_id("org/a", "charlotte's web")
    assert node == "ent:org/a:charlotte's web"
    assert parse_entity_node(node) == ("org/a", "charlotte's web")
    assert parse_entity_node("some-record-id") is None


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
    graph: SQLiteAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    projector = GraphProjector(graph, ON)
    record = _fact(make_record, "Melanie", "Charlotte's Web", trust=0.6)
    await projector.apply(write_event(record))
    mentions = {
        (e.dst, e.weight)
        for e in await graph.edges_of(record.record_id)
        if e.rel_type == MENTIONS_REL
    }
    assert mentions == {
        ("ent:default:melanie", 0.6),
        ("ent:default:charlotte's web", 0.6),
    }
    assert await graph.node_count() == 3


async def test_policy_off_projects_no_entities(
    graph: SQLiteAdjacencyGraph,
    projector: GraphProjector,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    await projector.apply(write_event(_fact(make_record, "Melanie", "Dune")))
    assert await graph.node_count() == 1 and await graph.edge_count() == 0


async def test_forget_cascades_mentions_and_orphaned_entities(
    graph: SQLiteAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    projector = GraphProjector(graph, ON)
    first = _fact(make_record, "Melanie", "Dune")
    second = _fact(make_record, "Melanie", "Emma")
    for record in (first, second):
        await projector.apply(write_event(record))
    await projector.apply(_forget(first))
    nodes = {n.node_id for n in await graph.neighbors(second.record_id)}
    assert nodes == {"ent:default:melanie", "ent:default:emma"}
    # "dune" lost its only mention and is gone; "melanie" is still mentioned.
    assert await graph.edges_of("ent:default:dune") == []
    assert await graph.node_count() == 3
    await projector.apply(_forget(second))
    assert await graph.node_count() == 0 and await graph.edge_count() == 0


async def test_rebuild_equals_incremental(
    graph: SQLiteAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    projector = GraphProjector(graph, ON)
    a = _fact(make_record, "Melanie", "Dune")
    b = _fact(make_record, "Caroline", "Denver")
    c = _fact(make_record, "Melanie", "Emma", trust=0.0)  # trust 0: no mention at all
    renamed = a.model_copy(update={"tags": ["kind:event", "rel:read", "dst:Persuasion"]})
    events = [
        write_event(a),
        write_event(b),
        write_event(c),
        link_event("default", a.record_id, b.record_id, "related", 0.7, "manual"),
        write_event(renamed),  # re-projected with another dst: "dune" goes stale
        _forget(b),
    ]
    for event in events:
        await projector.apply(event)
    incremental = await _state(graph)
    assert "ent:default:dune" not in {e[1] for e in incremental[0] if e[3] > 0}
    await projector.reset()
    for event in events:
        await projector.apply(event)
    assert await _state(graph) == incremental


async def test_namespaces_get_their_own_entity_nodes(
    graph: SQLiteAdjacencyGraph,
    make_record: Callable[..., MemoryRecord],
    write_event: Callable[..., MemoryEvent],
) -> None:
    projector = GraphProjector(graph, ON)
    in_a = _fact(make_record, "Melanie", None, namespace="a")
    in_b = _fact(make_record, "Melanie", None, namespace="b")
    for record in (in_a, in_b):
        await projector.apply(write_event(record))
    assert {e.dst for e in await graph.edge_list("a")} == {"ent:a:melanie"}
    assert {e.dst for e in await graph.edge_list("b")} == {"ent:b:melanie"}
    await projector.apply(_forget(in_a))
    assert [e.dst for e in await graph.edge_list("b")] == ["ent:b:melanie"]
    assert await graph.edges_of("ent:a:melanie") == []


async def test_mentions_is_a_reserved_rel(
    graph: SQLiteAdjacencyGraph,
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
