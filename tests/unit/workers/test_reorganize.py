"""D-42 reorganizer body, deterministically: community detection is
monkeypatched (fixed clusters) so the summary-write + membership-link +
supersession machinery is covered without the ``[community]`` extra.

Harness mirrors test_lifecycle_pipelines but adds the graph projection:
events land in BOTH the record projector and the graph projector — the same
append-then-project unit the engine's write door performs.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.config import constants
from memspine.config.loader import load_config
from memspine.core.events import EventKind, MemoryEvent
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.memories.associative.communities import PartitionResult, communities_available
from memspine.memories.associative.projector import GraphProjector
from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph
from memspine.services.storage.projector import RecordProjector
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers import pipelines
from memspine.workers.pipelines import PipelineContext, reorganize

NOW = datetime.now(UTC)


@pytest.fixture(autouse=True)
def _fresh_now(monkeypatch: pytest.MonkeyPatch) -> None:
    """``NOW`` is taken when each test starts, not when the module is imported: a long
    parallel run would otherwise age every "minutes / hours ago" offset (flake fix)."""
    monkeypatch.setattr(sys.modules[__name__], "NOW", datetime.now(UTC))


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

    def fake_partition(edges: object, **_knobs: object) -> PartitionResult:
        # Ignores resolution/randomness/seed/max_cluster_size/previous (A6, KB-12).
        labels = {node: i for i, cluster in enumerate(clusters) for node in cluster}
        return PartitionResult(labels=labels, mode="full")

    monkeypatch.setattr(pipelines, "partition_graph", fake_partition)


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
    # N2: a derived summary parent is never privileged, and never out-trusts a member.
    assert parent.source.role == constants.DERIVED_ROLE
    assert parent.trust <= min(m.trust for m in members)
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


async def test_leiden_runs_per_namespace_over_that_namespace_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KB-9: one detection per namespace, fed only that namespace's edges."""
    ctx, harness, _graph = await make_ctx()
    a = [await write(harness, f"a fact {i}. detail") for i in range(2)]
    b = [await write(harness, f"b fact {i}. detail", ns="agent/b") for i in range(2)]
    from memspine.memories.associative.links import link_event

    await harness.append(link_event("agent/a", a[0].record_id, a[1].record_id, "related", 1.0, "t"))
    await harness.append(link_event("agent/b", b[0].record_id, b[1].record_id, "related", 1.0, "t"))
    monkeypatch.setattr(pipelines, "communities_available", lambda: True)
    seen: list[set[str]] = []

    def fake_partition(edges: list[object], **_knobs: object) -> PartitionResult:
        seen.append({e.src for e in edges} | {e.dst for e in edges})  # type: ignore[attr-defined]
        return PartitionResult(labels={}, mode="full")

    monkeypatch.setattr(pipelines, "partition_graph", fake_partition)
    await reorganize(ctx)
    ids_a = {r.record_id for r in a}
    ids_b = {r.record_id for r in b}
    assert ids_a in seen and ids_b in seen
    assert all(not (s & ids_a and s & ids_b) for s in seen)


# -- KB-12 hybrid / #84 summary economy ------------------------------------------


def with_community(ctx: PipelineContext, **options: object) -> None:
    """Set ``memories.associative.policies.community`` on a test context."""
    ctx.config = load_config(
        user_config={"memories": {"associative": {"policies": {"community": options}}}}
    ).config


async def clique(harness: Harness, records: list[MemoryRecord], ns: str = "agent/a") -> None:
    from memspine.memories.associative.links import link_event

    for i, a in enumerate(records):
        for b in records[i + 1 :]:
            await harness.append(link_event(ns, a.record_id, b.record_id, "related", 1.0, "t"))


async def test_auto_without_the_extra_stays_a_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default ``algorithm: auto`` never falls back to LPA: without the extra
    the reorganizer is the D-40 no-op it always was."""
    ctx, harness, _graph = await make_ctx()
    await clique(harness, [await write(harness, f"fact {i}. detail") for i in range(3)])
    monkeypatch.setattr(pipelines, "communities_available", lambda: False)
    stats = await reorganize(ctx)
    assert stats["status"] == "skipped" and "community" in str(stats["reason"])
    with_community(ctx, algorithm="leiden")
    assert (await reorganize(ctx))["status"] == "skipped"
    assert await parents_of(ctx) == []


async def test_explicit_lpa_runs_without_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, harness, _graph = await make_ctx()
    members = [await write(harness, f"fact {i}. detail") for i in range(3)]
    await clique(harness, members)
    monkeypatch.setattr(pipelines, "communities_available", lambda: False)
    with_community(ctx, algorithm="lpa")
    stats = await reorganize(ctx)
    assert stats == {"status": "ok", "communities": 1, "parents": 1, "superseded": 0}


@pytest.mark.parametrize("algorithm", ["lpa", "leiden"])
async def test_a_summary_parent_never_joins_its_own_community(
    monkeypatch: pytest.MonkeyPatch, algorithm: str
) -> None:
    """Regression: the parent's community/derived_from edges fed back into the
    next partition, so every sweep re-summarised (and superseded) an unchanged
    community. Parents are no longer partition input or members."""
    if algorithm == "leiden" and not communities_available():
        pytest.skip("graspologic-native not installed ([community] extra)")
    ctx, harness, _graph = await make_ctx()
    await clique(harness, [await write(harness, f"fact {i}. detail") for i in range(4)])
    with_community(ctx, algorithm=algorithm)
    first = await reorganize(ctx)
    assert first == {"status": "ok", "communities": 1, "parents": 1, "superseded": 0}
    for _ in range(3):
        again = await reorganize(ctx)
        assert again == {"status": "ok", "communities": 1, "parents": 0, "superseded": 0}
    assert len(await parents_of(ctx)) == 1


async def test_near_identical_membership_keeps_its_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#84: Jaccard 5/6 >= 0.8 keeps the parent (no rewrite); its membership
    links follow the community."""
    ctx, harness, graph = await make_ctx()
    members = [await write(harness, f"community fact {i}. detail") for i in range(6)]
    ids = sorted(m.record_id for m in members)
    with_community(ctx, summary_keep_jaccard=0.8)
    force_communities(monkeypatch, [ids[:5]])
    assert (await reorganize(ctx))["parents"] == 1
    [parent] = await parents_of(ctx)
    force_communities(monkeypatch, [ids])
    stats = await reorganize(ctx)
    assert stats == {"status": "ok", "communities": 1, "parents": 0, "superseded": 0, "kept": 1}
    [still] = await parents_of(ctx)
    assert still.record_id == parent.record_id and still.status is RecordStatus.ACTIVATED
    linked = {
        e.src
        for e in await graph.edges_of(parent.record_id)
        if e.rel_type == "community" and e.weight > 0
    }
    assert linked == set(ids)
    # The kept parent is now idempotent against its own (unchanged) summary key.
    force_communities(monkeypatch, [ids[:5]])
    again = await reorganize(ctx)
    assert again["parents"] == 0 and again["superseded"] == 0
    linked = {
        e.src
        for e in await graph.edges_of(parent.record_id)
        if e.rel_type == "community" and e.weight > 0
    }
    assert linked == set(ids[:5])  # departed member's link tombstoned


async def test_jaccard_below_threshold_or_default_supersedes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ctx, harness, _graph = await make_ctx()
    members = [await write(harness, f"community fact {i}. detail") for i in range(8)]
    ids = sorted(m.record_id for m in members)
    force_communities(monkeypatch, [ids[:5]])
    await reorganize(ctx)
    force_communities(monkeypatch, [ids[:6]])  # 5/6 but the rule is off by default
    assert (await reorganize(ctx))["superseded"] == 1
    with_community(ctx, summary_keep_jaccard=0.8)
    force_communities(monkeypatch, [ids[:3] + ids[6:]])  # 3/8 vs the live parent
    stats = await reorganize(ctx)
    assert stats["parents"] == 1 and stats["superseded"] == 1 and stats["kept"] == 0


async def test_a_less_trusted_newcomer_forces_a_rewrite(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-47 §5: a kept summary must not stand for a member less trusted than it."""
    ctx, harness, _graph = await make_ctx()
    members = [await write(harness, f"community fact {i}. detail") for i in range(5)]
    low = MemoryRecord(
        namespace="agent/a",
        memory_type="episodic",
        content="low-trust newcomer. detail",
        trust=0.1,
        valid_from=NOW - timedelta(hours=2),
        recorded_at=NOW - timedelta(hours=2),
    )
    await harness.append(
        MemoryEvent(
            kind=EventKind.WRITE,
            namespace="agent/a",
            actor="test",
            payload={"record": low.model_dump(mode="json")},
        )
    )
    ids = sorted(m.record_id for m in members)
    with_community(ctx, summary_keep_jaccard=0.8)
    force_communities(monkeypatch, [ids])
    await reorganize(ctx)
    force_communities(monkeypatch, [sorted([*ids, low.record_id])])
    stats = await reorganize(ctx)
    assert stats["kept"] == 0 and stats["parents"] == 1 and stats["superseded"] == 1


async def test_collapse_keeps_existing_parents(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx, harness, _graph = await make_ctx()
    members = [await write(harness, f"community fact {i}. detail") for i in range(3)]
    force_communities(monkeypatch, [sorted(m.record_id for m in members)])
    await reorganize(ctx)

    def collapsed(edges: object, **_knobs: object) -> PartitionResult:
        return PartitionResult(labels={}, mode="full", collapsed=True)

    monkeypatch.setattr(pipelines, "partition_graph", collapsed)
    stats = await reorganize(ctx)
    assert stats == {"status": "ok", "communities": 0, "parents": 0, "superseded": 0}
    [parent] = await parents_of(ctx)
    assert parent.status is RecordStatus.ACTIVATED


async def test_incremental_mode_places_then_refreshes_and_rebuilds_from_the_log() -> None:
    """KB-12: the first sweep is a full build, later sweeps are incremental
    until ``refresh_every``; the state is folded from MARKER events, so a fresh
    index over the same log reproduces it (D0.1)."""
    ctx, harness, _graph = await make_ctx()
    a = [await write(harness, f"group a fact {i}. detail") for i in range(4)]
    b = [await write(harness, f"group b fact {i}. detail") for i in range(4)]
    await clique(harness, a)
    await clique(harness, b)
    with_community(ctx, algorithm="lpa", incremental=True, refresh_every=2, refresh_fraction=0.5)
    modes = [(await reorganize(ctx))["modes"]]
    newcomer = await write(harness, "group a newcomer. detail")
    from memspine.memories.associative.links import link_event

    await harness.append(
        link_event("agent/a", newcomer.record_id, a[0].record_id, "related", 1.0, "t")
    )
    second = await reorganize(ctx)
    modes += [second["modes"], (await reorganize(ctx))["modes"]]
    assert modes == [{"agent/a": "full"}, {"agent/a": "incremental"}, {"agent/a": "full"}]
    assert second["parents"] == 1 and second["superseded"] == 1  # a gained a member

    state = ctx.session_index.communities["agent/a"]
    await ctx.session_index.refresh(ctx.storage)
    rebuilt = pipelines.SessionIndex()
    await rebuilt.refresh(ctx.storage)
    assert rebuilt.communities["agent/a"] == ctx.session_index.communities["agent/a"]
    assert state.anchors[newcomer.record_id] == state.anchors[a[0].record_id]


async def test_incremental_refresh_fraction_triggers_a_full_build() -> None:
    ctx, harness, _graph = await make_ctx()
    a = [await write(harness, f"group a fact {i}. detail") for i in range(3)]
    await clique(harness, a)
    with_community(ctx, algorithm="lpa", incremental=True, refresh_fraction=0.1)
    await reorganize(ctx)
    from memspine.memories.associative.links import link_event

    newcomer = await write(harness, "newcomer. detail")
    await harness.append(
        link_event("agent/a", newcomer.record_id, a[0].record_id, "related", 1.0, "t")
    )
    # 1 placed node out of 4 > 10%: the incremental run is replaced by a refresh.
    assert (await reorganize(ctx))["modes"] == {"agent/a": "full"}


async def test_collapsed_incremental_run_falls_through_to_a_full_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """fix/graph-review #1: a collapsed incremental run must not lock the
    namespace — it falls through to a full run, which records a marker."""
    ctx, harness, _graph = await make_ctx()
    a = [await write(harness, f"group a fact {i}. detail") for i in range(4)]
    await clique(harness, a)
    with_community(ctx, algorithm="lpa", incremental=True, refresh_every=5)
    await reorganize(ctx)  # full build: incremental state exists from now on
    real = pipelines.partition_graph
    calls: list[str] = []

    def collapsing_incremental(edges: object, **knobs: object) -> PartitionResult:
        calls.append(str(knobs["mode"]))
        if knobs["mode"] == "incremental":
            return PartitionResult(labels={}, mode="incremental", collapsed=True)
        return real(edges, **knobs)  # type: ignore[arg-type]

    monkeypatch.setattr(pipelines, "partition_graph", collapsing_incremental)
    stats = await reorganize(ctx)
    assert calls == ["incremental", "full"]
    assert stats["modes"] == {"agent/a": "full"}


async def test_collapsed_runs_still_advance_the_sleep_counter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Even when every run collapses, the sleep counter advances, so
    ``refresh_every`` still fires (no permanent lock)."""
    ctx, harness, _graph = await make_ctx()
    a = [await write(harness, f"group a fact {i}. detail") for i in range(4)]
    await clique(harness, a)
    with_community(ctx, algorithm="lpa", incremental=True, refresh_every=5)
    await reorganize(ctx)
    await ctx.session_index.refresh(ctx.storage)
    anchors = dict(ctx.session_index.communities["agent/a"].anchors)

    def always_collapsed(edges: object, **knobs: object) -> PartitionResult:
        return PartitionResult(
            labels={},
            mode=knobs["mode"],
            collapsed=True,  # type: ignore[arg-type]
        )

    monkeypatch.setattr(pipelines, "partition_graph", always_collapsed)
    await reorganize(ctx)
    await reorganize(ctx)
    await ctx.session_index.refresh(ctx.storage)
    state = ctx.session_index.communities["agent/a"]
    assert state.sleeps == 2
    assert state.anchors == anchors  # a collapse changes no membership
