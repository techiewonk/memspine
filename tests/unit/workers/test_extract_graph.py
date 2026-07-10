"""C2: the extract_graph pipeline body, deterministically.

A fake ``extract_edges`` callable stands in for the LLM (offline), so the
WRITE-fact + asserted-LINK + idempotency + confidence-gate + no-feedback-loop
machinery is covered without a model. Mirrors test_reorganize's harness: events
land in both the record and graph projectors — the engine's write-door unit.
"""

from __future__ import annotations

from datetime import UTC, datetime

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord
from memspine.memories.associative.projector import GraphProjector
from memspine.prompts.models import ExtractedEdge
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers.pipelines import PipelineContext, extract_graph

NOW = datetime.now(UTC)


class Harness:
    def __init__(self, storage: SQLiteStorage, graph: SQLiteAdjacencyGraph) -> None:
        self.storage = storage
        self.projectors = [RecordProjector(storage), GraphProjector(graph)]

    async def append(self, event: MemoryEvent) -> None:
        appended = await self.storage.append_event(event)
        assert appended.seq is not None
        for projector in self.projectors:
            await projector.apply(appended)
            await self.storage.set_offset(projector.name, appended.seq)


async def _make(
    edges: list[ExtractedEdge],
    calls: list[str] | None = None,
    semantic_policies: dict[str, object] | None = None,
):
    from memspine.config.loader import load_config

    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    graph = SQLiteAdjacencyGraph(client)
    harness = Harness(storage, graph)

    async def fake_extract(content: str) -> list[ExtractedEdge]:
        if calls is not None:
            calls.append(content)
        return list(edges)

    config = load_config(
        overrides={"memories": {"semantic": {"enabled": True, "policies": semantic_policies or {}}}}
    ).config
    ctx = PipelineContext(
        storage=storage,
        config=config,
        append_event=harness.append,
        graph=graph,
        extract_edges=fake_extract,
    )
    return ctx, harness, graph


async def _seed(harness: Harness, content: str, ns: str = "agent/a") -> MemoryRecord:
    record = MemoryRecord(namespace=ns, memory_type="episodic", content=content)
    await harness.append(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor="user",
            payload={"record": record.model_dump(mode="json")},
        )
    )
    return record


async def test_disabled_without_extract_edges_callable() -> None:
    ctx, _harness, _graph = await _make([])
    ctx.extract_edges = None
    result = await extract_graph(ctx)
    assert result["status"] == "skipped"


async def test_edge_becomes_a_fact_record_and_asserted_link() -> None:
    edge = ExtractedEdge(
        src_entity="Alice",
        rel="works_at",
        dst_entity="Acme",
        fact="Alice works at Acme",
        confidence=0.9,
    )
    ctx, harness, graph = await _make([edge])
    source = await _seed(harness, "Alice works at Acme.")

    result = await extract_graph(ctx)
    assert result["status"] == "ok"
    assert result["edges_written"] == 1
    assert result["links"] == 1

    facts = [
        r
        for r in await harness.storage.list_records("agent/a", "semantic")
        if r.source.channel == "extract_graph"
    ]
    assert len(facts) == 1
    fact = facts[0]
    assert fact.entity == "Alice" and fact.attribute == "works_at"
    assert fact.content == "Alice works at Acme"
    # An asserted edge links the source record to the new fact.
    edges = await graph.edges_of(source.record_id)
    asserted = [e for e in edges if e.rel_type == "asserted" and e.weight > 0]
    assert asserted and asserted[0].dst == fact.record_id


async def test_rerun_is_idempotent() -> None:
    edge = ExtractedEdge(
        src_entity="Alice",
        rel="works_at",
        dst_entity="Acme",
        fact="Alice works at Acme",
        confidence=0.9,
    )
    ctx, harness, _graph = await _make([edge])
    await _seed(harness, "Alice works at Acme.")

    first = await extract_graph(ctx)
    assert first["edges_written"] == 1
    second = await extract_graph(ctx)
    # The (src, rel, dst) key already exists -> nothing new written.
    assert second["edges_written"] == 0
    assert second["skipped_existing"] >= 1
    facts = [
        r
        for r in await harness.storage.list_records("agent/a", "semantic")
        if r.source.channel == "extract_graph"
    ]
    assert len(facts) == 1  # not duplicated


async def test_never_extracts_from_its_own_output() -> None:
    """The fact records extract_graph writes must not seed a second round."""
    edge = ExtractedEdge(
        src_entity="Alice",
        rel="works_at",
        dst_entity="Acme",
        fact="Alice works at Acme",
        confidence=0.9,
    )
    calls: list[str] = []
    ctx, harness, _graph = await _make([edge], calls=calls)
    await _seed(harness, "Alice works at Acme.")

    await extract_graph(ctx)
    calls.clear()
    await extract_graph(ctx)
    # Second run: only the original source is re-read, never the extract_graph fact.
    assert all(c == "Alice works at Acme." for c in calls)
    assert "Alice works at Acme" not in calls or all(c.endswith(".") for c in calls)


async def test_low_confidence_edges_are_filtered() -> None:
    edges = [
        ExtractedEdge(src_entity="A", rel="r", dst_entity="B", fact="A r B", confidence=0.2),
        ExtractedEdge(src_entity="C", rel="r", dst_entity="D", fact="C r D", confidence=0.95),
    ]
    ctx, harness, _graph = await _make(
        edges, semantic_policies={"extract_graph": {"min_confidence": 0.5}}
    )
    await _seed(harness, "some source text")

    result = await extract_graph(ctx)
    assert result["edges_written"] == 1  # only the 0.95 edge survives
    facts = [
        r
        for r in await harness.storage.list_records("agent/a", "semantic")
        if r.source.channel == "extract_graph"
    ]
    assert {f.entity for f in facts} == {"C"}
