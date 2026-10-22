"""RM-104: the filtered graph leg fetches only its seeds' edges (``frontier_edges``).

``one_hop`` reads only edges with an endpoint in the seed set, so the seed-scoped fetch
must give the leg exactly the hits of the namespace scan (``edge_list``). Proved on the
RM-93 world (end to end through ``Engine.search`` and on the leg's edge selection) and
on randomized graphs (SQLite adjacency always, LadybugDB when installed). The unfiltered
read keeps the namespace scan (MF-07 readiness and the L3 artefact count are
namespace-wide).
"""

from __future__ import annotations

import random
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import test_rm88_rm91_graph_caller as caller_tests
from test_rm93_graph_filter import NS, QUERY, _build, _engine, _Spy

from memspine.clients.sqlite import SQLiteClient
from memspine.derive.pipeline.stages.edge_compiler_v2 import compile_graph_edges
from memspine.read.legs.graph import HopEdge, HopResult, one_hop
from memspine.read.pipeline import legs as pipeline_legs
from memspine.services.graph.qualifiers import (
    AssertionModality,
    ClaimState,
    EdgeQualifiers,
    ErasureState,
    QualifierFilter,
    graph_filter_scope,
    with_qualifiers,
)
from memspine.services.storage.sqlite.schema import metadata

LATER = datetime.now(UTC) + timedelta(days=1)

FILTERS: list[QualifierFilter] = [
    QualifierFilter(assertion_modalities=frozenset({AssertionModality.ASSERTED})),
    QualifierFilter(known_as_of=LATER),
    QualifierFilter(known_as_of=datetime(2000, 1, 1, tzinfo=UTC)),
    QualifierFilter(topical_domains=frozenset({"health"})),
    QualifierFilter(claim_states=frozenset(ClaimState)),
]


def _hop_edges(edges: list[Any]) -> list[HopEdge]:
    return [
        HopEdge(
            e.src,
            e.dst,
            str(e.properties.get("record_id") or ""),
            bool(e.properties.get("tentative")),
            bool(e.properties.get("closed")),
        )
        for e in edges
    ]


def _walk(seeds: list[str], edges: list[Any], live: set[str]) -> HopResult:
    return one_hop(seeds, _hop_edges(edges), min_seeds=2, admit_record=live)


def _same_hits(a: HopResult, b: HopResult) -> None:
    assert a.scores == b.scores
    assert a.convergent == b.convergent
    assert a.reached == b.reached


# ------------------------------------------------------------------ the RM-93 world


@pytest.mark.parametrize("flt", FILTERS, ids=[f"f{i}" for i in range(len(FILTERS))])
async def test_rm93_world_frontier_matches_namespace_scan(flt: QualifierFilter) -> None:
    eng = await _engine()
    try:
        await _build(eng)
        live = list(await eng._storage.list_records(NS))
        by_id = {r.record_id: r for r in live}
        seeds = list(await eng._graph_seeds(NS, QUERY))
        assert len(seeds) >= 2
        scan = await pipeline_legs._graph_leg_edges(eng, NS, seeds, by_id, flt, use_frontier=False)
        front = await pipeline_legs._graph_leg_edges(eng, NS, seeds, by_id, flt, use_frontier=True)
        auto = await pipeline_legs._graph_leg_edges(eng, NS, seeds, by_id, flt)
        unfiltered = await pipeline_legs._graph_leg_edges(eng, NS, seeds, by_id, None)
    finally:
        await eng.stop()
    assert (scan.scope, front.scope, auto.scope, unfiltered.scope) == (
        "namespace",
        "seeds",
        "seeds",
        "namespace",
    )
    ids = set(by_id)
    _same_hits(_walk(seeds, scan.edges, ids), _walk(seeds, front.edges, ids))
    # every seed-incident admitted edge of the scan is in the frontier set, and nothing else
    seed_set = set(seeds)
    incident = {(e.src, e.dst, e.rel_type) for e in scan.edges if {e.src, e.dst} & seed_set}
    assert {(e.src, e.dst, e.rel_type) for e in front.edges} == incident


async def test_search_results_identical_with_and_without_frontier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    flt = QualifierFilter(
        assertion_modalities=frozenset({AssertionModality.ASSERTED}), known_as_of=LATER
    )
    eng = await _engine()
    spy = _Spy(monkeypatch)
    runs: list[list[Any]] = []
    try:
        await _build(eng)
        for frontier in (False, True, False):
            monkeypatch.setattr(pipeline_legs, "_frontier_capable", lambda g, f=frontier: f)
            hits = await eng.search(QUERY, namespace=NS, top_k=8, graph_filter=flt)
            runs.append([(rec.record_id, round(score, 6)) for rec, score in hits])
    finally:
        await eng.stop()
    assert runs[0] and runs[0] == runs[1] == runs[2]
    scopes = [e["scope"] for e in spy.of("read.graph_filter")]
    assert scopes == ["namespace", "seeds", "namespace"]  # each search ran the leg (no cache)
    hops = spy.of("read.graph_hop")
    assert len(hops) == 3 and hops[0]["convergent"] == hops[1]["convergent"] != []
    assert hops[0]["records"] == hops[1]["records"]


async def test_filtered_leg_uses_frontier_and_reports_seed_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    calls: list[str] = []
    try:
        await _build(eng)
        graph = eng._graph
        real_list, real_front = graph.edge_list, graph.frontier_edges

        async def edge_list(*a: Any, **k: Any) -> Any:
            calls.append("edge_list")
            return await real_list(*a, **k)

        async def frontier_edges(*a: Any, **k: Any) -> Any:
            calls.append("frontier_edges")
            return await real_front(*a, **k)

        monkeypatch.setattr(graph, "edge_list", edge_list)
        monkeypatch.setattr(graph, "frontier_edges", frontier_edges)
        flt = QualifierFilter(
            assertion_modalities=frozenset({AssertionModality.ASSERTED}), known_as_of=LATER
        )
        await eng.search(QUERY, namespace=NS, top_k=4, graph_filter=flt)
        filtered_calls = list(calls)
        calls.clear()
        await eng.search(QUERY, namespace=NS, top_k=4)
    finally:
        await eng.stop()
    # the leg's namespace scan is replaced by one seed fetch. RM-110: the seed lookup's
    # entity index is built by one edge_list on the first graph read and then maintained,
    # so the second read's only edge_list is the unfiltered leg's own namespace scan.
    assert filtered_calls.count("frontier_edges") == 1
    assert "frontier_edges" not in calls  # unfiltered: unchanged
    assert filtered_calls.count("edge_list") == 1  # the entity index build (first read)
    assert calls.count("edge_list") == 1  # the unfiltered leg; the index is cached
    (seen,) = spy.of("read.graph_filter")
    assert seen["scope"] == "seeds" and 0 < seen["admitted"] < seen["edges"]
    hop = spy.of("read.graph_hop")
    assert hop and hop[0]["convergent"] == [f"ent:{NS}:bram"]


async def test_frontier_edges_missing_from_the_read_are_looked_up() -> None:
    """RM-88 under the frontier fetch: a source record outside the read's set is fetched
    from storage for the caller check, exactly as on the namespace scan."""
    eng = await _engine()
    flt = QualifierFilter(known_as_of=LATER)
    try:
        await _build(eng)
        seeds = list(await eng._graph_seeds(NS, QUERY))
        live = {r.record_id for r in await eng._storage.list_records(NS)}
        with graph_filter_scope(flt):
            front = await pipeline_legs._graph_leg_edges(eng, NS, seeds, {}, flt)
            scan = await pipeline_legs._graph_leg_edges(eng, NS, seeds, {}, flt, use_frontier=False)
    finally:
        await eng.stop()
    assert front.edges and all(str(e.properties["record_id"]) in live for e in front.edges)
    _same_hits(_walk(seeds, scan.edges, live), _walk(seeds, front.edges, live))


async def test_frontier_caller_scope_grant_and_revoke(monkeypatch: pytest.MonkeyPatch) -> None:
    """RM-88 on the seed-scoped fetch: a seed-incident edge scoped to another namespace is
    hidden until that namespace grants the caller, and hidden again once revoked."""
    eng = await caller_tests._engine()
    spy = caller_tests._Spy(monkeypatch)
    try:
        await caller_tests._build(eng, "a")
        evidence = await eng.write("Ilse is Bram's cousin", namespace="b", memory_type="semantic")
        # touches the seed ent:a:ilse; scoped to b
        await caller_tests._put_edge(eng, "a", "ent:a:ilse", "ent:b:bram", "b", evidence.record_id)
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=caller_tests._flt())
        before = spy.last()
        await eng.grant("a", namespace="b")  # b lets a read it
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=caller_tests._flt())
        granted = spy.last()
        await eng.revoke("a", namespace="b")
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=caller_tests._flt())
        revoked = spy.last()
    finally:
        await eng.stop()
    assert before["scope"] == granted["scope"] == "seeds"
    assert before["caller_hidden"] == 1 and revoked["caller_hidden"] == 1
    assert granted["caller_hidden"] == 0 and granted["admitted"] == before["admitted"] + 1
    assert revoked["admitted"] == before["admitted"]


async def test_frontier_record_viewer_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    """RM-88 second layer on the seed-scoped fetch: a participants-only source record hides
    its seed-incident edges from an outsider viewer."""
    eng = await caller_tests._engine()
    spy = caller_tests._Spy(monkeypatch)
    try:
        private = await caller_tests._fact(
            eng,
            "a",
            "Ilse",
            "knows",
            "Bram",
            "Ilse introduced Bram to the chess club",
            participants=["Ilse", "Tomasz"],
            visibility="participants",
        )
        await caller_tests._fact(
            eng, "a", "Tomasz", "employs", "Bram", "Tomasz hired Bram as a carpenter"
        )
        await compile_graph_edges(eng._pipeline_ctx())
        assert eng._graph is not None
        private_edges = sum(
            1
            for e in await eng._graph.frontier_edges(["ent:a:ilse", "ent:a:tomasz"], namespace="a")
            if e.properties.get("record_id") == private
        )
        flt = caller_tests._flt()
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=flt, viewer="Ilse")
        member = spy.last()
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=flt, viewer="Dana")
        outsider = spy.last()
    finally:
        await eng.stop()
    assert private_edges > 0 and member["scope"] == "seeds"
    assert member["caller_hidden"] == 0 and outsider["caller_hidden"] == private_edges
    assert outsider["admitted"] == member["admitted"] - private_edges


# ------------------------------------------------------------------ randomized graphs

RNS = "rand/ns"
OTHER = "rand/other"
SCOPES = (RNS, OTHER, None)


def _random_graph(rng: random.Random, n_nodes: int, n_edges: int) -> list[tuple[Any, ...]]:
    out: list[tuple[Any, ...]] = []
    seen: set[tuple[str, str, str]] = set()
    t0 = datetime(2025, 1, 1, tzinfo=UTC)
    while len(out) < n_edges:
        a, b = rng.randrange(n_nodes), rng.randrange(n_nodes)
        if rng.random() < 0.3:
            a = rng.randrange(3)  # a few hubs
        rel = rng.choice(("knows", "employs", "related"))
        key = (f"n{a}", f"n{b}", rel)
        if key in seen:
            continue
        seen.add(key)
        props: dict[str, Any] = {
            "weight": rng.choice((0.0, 0.4, 0.8, 1.0)),
            "compiled": "edge_compiler_v2" if rng.random() < 0.9 else "other",
            "tentative": rng.random() < 0.1,
            "closed": rng.random() < 0.05,
        }
        if rng.random() < 0.95:
            props["record_id"] = f"r{rng.randrange(n_edges // 2)}"
        if rng.random() < 0.9:
            start = t0 + timedelta(days=rng.randrange(400))
            quals = EdgeQualifiers(
                valid_from=start,
                valid_open_ended=True,
                known_at=start,
                assertion_modality=rng.choice(list(AssertionModality)),
                claim_state=rng.choice(list(ClaimState)),
                erasure_state=rng.choice(
                    [ErasureState.ACTIVE] * 8 + [ErasureState.ERASED, ErasureState.ERASURE_PENDING]
                ),
                scope=rng.choice(SCOPES),
                topical_domain=rng.choice(("work", "health", "health/condition", None)),
            )
            props = with_qualifiers(props, quals)
        ns = RNS if rng.random() < 0.85 else OTHER
        out.append((*key, props, ns))
    return out


RANDOM_FILTERS: list[QualifierFilter] = [
    QualifierFilter(assertion_modalities=frozenset({AssertionModality.ASSERTED})),
    QualifierFilter(
        claim_states=frozenset({ClaimState.CURRENT}),
        known_as_of=datetime(2025, 9, 1, tzinfo=UTC),
        valid_at=datetime(2025, 10, 1, tzinfo=UTC),
    ),
    QualifierFilter(topical_domains=frozenset({"health"})),
    QualifierFilter(exclude_claim_states=frozenset({ClaimState.RETRACTED})),
]


@pytest.fixture(params=["sql_adjacency", "ladybug"])
async def graph(request: pytest.FixtureRequest) -> AsyncIterator[Any]:
    if request.param == "ladybug":
        pytest.importorskip("ladybug")
        from memspine.clients.ladybug import LadybugClient
        from memspine.services.graph.ladybug import LadybugGraphStore

        lb = LadybugClient(":memory:")
        await lb.connect()
        yield LadybugGraphStore(lb)
        await lb.close()
        return
    from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph

    client = SQLiteClient(":memory:")
    await client.connect()
    async with client.engine.begin() as conn:
        await conn.run_sync(metadata.create_all)
    yield SqlAdjacencyGraph(client)
    await client.close()


class _NoStorage:
    async def get_record(self, record_id: str) -> None:
        return None  # a record missing from the read's set fails the caller check


@pytest.mark.parametrize("world", [1, 2, 3])
async def test_randomized_graph_frontier_matches_namespace_scan(graph: Any, world: int) -> None:
    rng = random.Random(1040 + world)
    data = _random_graph(rng, n_nodes=40, n_edges=260)
    for src, dst, rel, props, ns in data:
        await graph.upsert_edge(src, dst, rel, props, namespace=ns)
    records = {f"r{i}" for i in range(130) if rng.random() < 0.8}
    by_id: dict[str, Any] = {rid: SimpleNamespace(record_id=rid, tags=[]) for rid in records}
    eng = SimpleNamespace(_graph=graph, _shared=None, _storage=_NoStorage())
    compared = 0
    for flt in RANDOM_FILTERS:
        for _ in range(6):
            seeds = [f"n{rng.randrange(40)}" for _ in range(rng.randrange(0, 5))]
            scan = await pipeline_legs._graph_leg_edges(
                eng, RNS, seeds, by_id, flt, use_frontier=False
            )
            front = await pipeline_legs._graph_leg_edges(
                eng, RNS, seeds, by_id, flt, use_frontier=True
            )
            for min_seeds in (1, 2):
                old = one_hop(
                    seeds, _hop_edges(scan.edges), min_seeds=min_seeds, admit_record=records
                )
                new = one_hop(
                    seeds, _hop_edges(front.edges), min_seeds=min_seeds, admit_record=records
                )
                _same_hits(old, new)
                compared += bool(old.scores)
    assert compared > 0  # the worlds do produce hits
