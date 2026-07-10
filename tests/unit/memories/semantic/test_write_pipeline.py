"""C3: synchronous graphiti write pipeline (ADR-026).

A fake ``extract_edges`` stands in for the LLM. Proves: single mode (no
pipeline) is unchanged; graph mode writes each edge as a fact-keyed record
through the same door (so dedup + the conflict ladder apply); an edge record
never recurses; edge facts inherit source trust (E1).
"""

from __future__ import annotations

from memspine.clients.sqlite import SQLiteClient
from memspine.core.events import MemoryEvent
from memspine.core.policies.conflict import ConflictPolicy
from memspine.core.policies.dedup import DedupPolicy
from memspine.core.records import MemoryRecord
from memspine.memories.semantic.store import SemanticMemory
from memspine.memories.semantic.write_pipeline import EDGE_CHANNEL, GraphWritePipeline
from memspine.prompts.models import ExtractedEdge
from memspine.services.embedding.hash_local import HashEmbedding
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage


async def _memory(edges: list[ExtractedEdge] | None):
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    projector = RecordProjector(storage)

    async def append(event: MemoryEvent) -> None:
        appended = await storage.append_event(event)
        assert appended.seq is not None
        await projector.apply(appended)
        await storage.set_offset(projector.name, appended.seq)

    calls: list[str] = []

    async def fake_extract(content: str) -> list[ExtractedEdge]:
        calls.append(content)
        return list(edges or [])

    pipeline = GraphWritePipeline(fake_extract) if edges is not None else None
    mem = SemanticMemory(
        storage=storage,
        embedder=HashEmbedding(),
        append_event=append,
        conflict=ConflictPolicy.bind({}),
        dedup=DedupPolicy.bind({}),
        write_pipeline=pipeline,
    )
    return mem, storage, calls


async def test_single_mode_writes_only_the_primary_record() -> None:
    mem, storage, _calls = await _memory(None)  # no pipeline => single pass
    await mem.write(
        MemoryRecord(namespace="a", memory_type="semantic", content="Alice works at Acme")
    )
    records = await storage.list_records("a", "semantic")
    assert len(records) == 1
    assert all(r.source.channel != EDGE_CHANNEL for r in records)


async def test_graph_mode_writes_edge_facts_through_the_door() -> None:
    edge = ExtractedEdge(
        src_entity="Alice",
        rel="works_at",
        dst_entity="Acme",
        fact="Alice works at Acme",
        confidence=0.9,
    )
    mem, storage, _calls = await _memory([edge])
    src = await mem.write(
        MemoryRecord(namespace="a", memory_type="semantic", content="Alice joined Acme", trust=0.7)
    )
    records = await storage.list_records("a", "semantic")
    edge_facts = [r for r in records if r.source.channel == EDGE_CHANNEL]
    assert len(edge_facts) == 1
    fact = edge_facts[0]
    assert fact.entity == "Alice" and fact.attribute == "works_at"
    # E1: derived edge fact never out-trusts its source.
    assert fact.trust <= src.record.trust


async def test_edge_record_does_not_recurse() -> None:
    edge = ExtractedEdge(
        src_entity="Alice",
        rel="works_at",
        dst_entity="Acme",
        fact="Alice works at Acme",
        confidence=0.9,
    )
    mem, _storage, calls = await _memory([edge])
    await mem.write(
        MemoryRecord(namespace="a", memory_type="semantic", content="Alice joined Acme")
    )
    # extract_edges runs once for the primary; the edge record (channel=
    # write_pipeline) is guarded, so no second extraction call.
    assert len(calls) == 1


async def test_edge_fact_climbs_the_conflict_ladder() -> None:
    """A superseding edge invalidates the prior one via the existing M4 ladder."""
    first = ExtractedEdge(
        src_entity="Alice",
        rel="works_at",
        dst_entity="Acme",
        fact="Alice works at Acme",
        confidence=0.9,
    )
    mem, storage, _calls = await _memory([first])
    await mem.write(MemoryRecord(namespace="a", memory_type="semantic", content="Alice at Acme"))

    # Swap the fake to now assert a *different* employer for the same key.
    second = ExtractedEdge(
        src_entity="Alice",
        rel="works_at",
        dst_entity="Globex",
        fact="Alice works at Globex",
        confidence=0.9,
    )
    mem._write_pipeline = GraphWritePipeline(lambda _c: _async([second]))  # type: ignore[assignment]
    await mem.write(
        MemoryRecord(namespace="a", memory_type="semantic", content="Alice moved to Globex")
    )

    active = [
        r
        for r in await storage.list_records("a", "semantic")
        if r.source.channel == EDGE_CHANNEL and r.status.value == "activated"
    ]
    # Exactly one active (entity=Alice, attribute=works_at) fact — the ladder
    # archived the superseded edge rather than keeping two active values.
    assert len(active) == 1
    assert "Globex" in active[0].content


async def _async(edges: list[ExtractedEdge]):
    return edges
