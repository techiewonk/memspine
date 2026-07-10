"""D-42 reorganizer body, deterministically: community detection is
monkeypatched (fixed clusters) so the summary-write + membership-link +
supersession machinery is covered without the ``[community]`` extra.

Harness mirrors test_lifecycle_pipelines but adds the graph projection:
events land in BOTH the record projector and the graph projector — the same
append-then-project unit the engine's write door performs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import load_config
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.associative.projector import GraphProjector
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers import pipelines
from memspine.workers.pipelines import PipelineContext, reorganize

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


async def make_ctx() -> tuple[PipelineContext, Harness, SQLiteAdjacencyGraph]:
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client)
    await storage.start()
    graph = SQLiteAdjacencyGraph(client)
    harness = Harness(storage, graph)
    ctx = PipelineContext(
        storage=storage,
        config=load_config().config,
        append_event=harness.append,
        graph=graph,
    )
    return ctx, harness, graph


async def write(harness: Harness, content: str, ns: str = "agent/a") -> MemoryRecord:
    record = MemoryRecord(
        namespace=ns,
        memory_type="episodic",
        content=content,
        valid_from=NOW - timedelta(hours=2),
        recorded_at=NOW - timedelta(hours=2),
    )
    await harness.append(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace=ns,
            actor="test",
            payload={"record": record.model_dump(mode="json")},
        )
    )
    return record


def force_communities(monkeypatch: pytest.MonkeyPatch, clusters: list[list[str]]) -> None:
    """Deterministic community detection: the extra is 'installed' and Leiden
    always answers ``clusters`` — the reorganize body runs for real."""
    monkeypatch.setattr(pipelines, "communities_available", lambda: True)

    def fake_detect(edges: object, **_knobs: object) -> list[list[str]]:
        return clusters  # ignores min_size/resolution/randomness/seed/max_cluster_size (A6)

    monkeypatch.setattr(pipelines, "detect_communities", fake_detect)


async def parents_of(ctx: PipelineContext, ns: str = "agent/a") -> list[MemoryRecord]:
    return [
        record
        for record in await ctx.storage.list_records(ns, "semantic")
        if record.source.channel == "reorganize"
    ]


async def test_reorganize_writes_summary_parent_and_membership_links(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, harness, graph = await make_ctx()
    members = [await write(harness, f"community fact {i}. detail") for i in range(3)]
    force_communities(monkeypatch, [sorted(m.record_id for m in members)])

    stats = await reorganize(ctx)
    assert stats == {"status": "ok", "communities": 1, "parents": 1, "superseded": 0}
    [parent] = await parents_of(ctx)
    assert parent.status is RecordStatus.ACTIVATED
    community_edges = [
        edge for edge in await graph.edges_of(parent.record_id) if edge.rel_type == "community"
    ]
    assert {edge.src for edge in community_edges} == {m.record_id for m in members}
    assert all(edge.weight == 1.0 for edge in community_edges)

    # Idempotence: unchanged membership is skipped, no second parent.
    rerun = await reorganize(ctx)
    assert rerun == {"status": "ok", "communities": 1, "parents": 0, "superseded": 0}
    assert len(await parents_of(ctx)) == 1


async def test_membership_drift_supersedes_and_tombstones_old_community_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H2: two sweeps with shifted membership — the stale parent is archived
    AND its member→parent community edges are tombstoned (weight 0); the fresh
    parent's stay live."""
    ctx, harness, graph = await make_ctx()
    members = [await write(harness, f"community fact {i}. detail") for i in range(3)]
    force_communities(monkeypatch, [sorted(m.record_id for m in members)])
    await reorganize(ctx)
    [old_parent] = await parents_of(ctx)

    drifted = await write(harness, "late-arriving member. detail")
    shifted = sorted([members[0].record_id, members[1].record_id, drifted.record_id])
    force_communities(monkeypatch, [shifted])
    stats = await reorganize(ctx)
    assert stats == {"status": "ok", "communities": 1, "parents": 1, "superseded": 1}

    refreshed_old = await ctx.storage.get_record(old_parent.record_id)
    assert refreshed_old is not None and refreshed_old.status is RecordStatus.ARCHIVED
    old_edges = [
        edge for edge in await graph.edges_of(old_parent.record_id) if edge.rel_type == "community"
    ]
    assert old_edges and all(edge.weight == 0.0 for edge in old_edges)  # all tombstoned
    new_parent = next(
        record for record in await parents_of(ctx) if record.status is RecordStatus.ACTIVATED
    )
    new_edges = [
        edge for edge in await graph.edges_of(new_parent.record_id) if edge.rel_type == "community"
    ]
    assert {edge.src for edge in new_edges} == set(shifted)
    assert all(edge.weight == 1.0 for edge in new_edges)
    # ...and the archived parent lost its *membership* reach; only the
    # derived_from provenance facts (never tombstoned) still touch it.
    assert await graph.neighbors(old_parent.record_id, rel_type="community") == []


async def test_one_failing_community_yields_partial_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, harness, _graph = await make_ctx()
    healthy = [await write(harness, f"healthy fact {i}. detail") for i in range(3)]
    broken = [await write(harness, f"broken fact {i}. detail") for i in range(3)]
    force_communities(
        monkeypatch,
        [sorted(m.record_id for m in healthy), sorted(m.record_id for m in broken)],
    )
    poisoned_id = broken[0].record_id
    real_get = ctx.storage.get_record

    async def failing_get(record_id: str) -> MemoryRecord | None:
        if record_id == poisoned_id:
            raise RuntimeError("storage hiccup")
        return await real_get(record_id)

    monkeypatch.setattr(ctx.storage, "get_record", failing_get)
    stats = await reorganize(ctx)
    assert stats["status"] == "partial"
    errors = stats["errors"]
    assert isinstance(errors, list) and len(errors) == 1 and "storage hiccup" in errors[0]
    assert stats["parents"] == 1  # the healthy community still got its parent


async def test_cross_namespace_community_is_an_error_not_a_crash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, harness, _graph = await make_ctx()
    ours = [await write(harness, f"our fact {i}. detail") for i in range(2)]
    theirs = await write(harness, "their fact. detail", ns="agent/b")
    force_communities(monkeypatch, [sorted([*(m.record_id for m in ours), theirs.record_id])])
    stats = await reorganize(ctx)
    assert stats["status"] == "partial"
    errors = stats["errors"]
    assert isinstance(errors, list) and "namespaces" in errors[0]
    assert stats["parents"] == 0
    assert await parents_of(ctx) == [] and await parents_of(ctx, "agent/b") == []


async def test_default_context_lock_is_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """M5: contexts built directly (no engine) default to a no-op lock — the
    pipeline must not require engine plumbing to run."""
    ctx, _harness, _graph = await make_ctx()
    async with ctx.lock("agent/a"):
        pass  # must not raise, block, or need an event-loop-bound Lock
