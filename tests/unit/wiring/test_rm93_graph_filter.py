"""RM-92/RM-93: compiled edges carry typed qualifiers; searches filter graph traversal by them.

Model-free. The world is the R3-05b one (Ilse and Tomasz both connect to Bram; hypothetical
links to Dana). RM-82/RM-83 guards are checked on the qualifier model directly.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.derive.pipeline.stages.edge_compiler_v2 import compile_graph_edges
from memspine.observability import tap
from memspine.read.pipeline import legs as pipeline_legs
from memspine.read.pipeline import request as read_request
from memspine.services.graph.qualifiers import (
    AssertionModality,
    EdgeQualifiers,
    ErasureState,
    QualifierFilter,
    active_graph_filter,
    edge_qualifiers,
    graph_filter_scope,
)
from memspine.store.records import SourceInfo

NS = "a"
T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
QUERY = "How are Ilse and Tomasz connected"
TURNS = {
    "ilse": "Ilse introduced Bram to the chess club",
    "tom": "Tomasz hired Bram as a carpenter last spring",
    "dana_i": "Ilse might invite Dana to the chess club",
    "dana_t": "Tomasz might hire Dana as an apprentice",
}


def _node(name: str) -> str:
    return f"ent:{NS}:{name.lower()}"


async def _engine(**read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        graph={"edge_compiler_v2": True},
        read={
            "hybrid": True,
            "record_access": False,
            "legs_v2": True,
            "graph_leg_v2": True,
            "graph_leg_seed_hits": 0,
            **read,
        },
    )
    await eng.start()
    return eng


async def _build(eng: Engine) -> dict[str, str]:
    ids: dict[str, str] = {}
    for key, text in TURNS.items():
        rec = await eng.write(text, namespace=NS, memory_type="episodic", valid_from=T0)
        ids[key] = rec.record_id

    async def fact(key: str, src: str, rel: str, dst: str, turn: str, *tags: str) -> None:
        rec = await eng.write(
            f"{src} {rel} {dst}",
            namespace=NS,
            memory_type="semantic",
            entity=src,
            tags=["kind:event", f"rel:{rel}", f"dst:{dst}", *tags],
            source=SourceInfo(role="derived", channel="extract_graph", parents=[ids[turn]]),
            valid_from=T0,
        )
        ids[key] = rec.record_id

    await fact("f_ilse", "Ilse", "knows", "Bram", "ilse")
    await fact("f_tom", "Tomasz", "employs", "Bram", "tom")
    await fact("f_dana_i", "Ilse", "may_invite", "Dana", "dana_i", "hypothetical")
    await fact("f_dana_t", "Tomasz", "may_hire", "Dana", "dana_t", "hypothetical")
    stats = await compile_graph_edges(eng._pipeline_ctx())
    assert stats["status"] == "ok"
    return ids


class _Spy:
    """Captures tap events and the legs handed to fusion."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

        def event(stage: str, /, **fields: Any) -> None:
            self.events.append((stage, fields))

        monkeypatch.setattr(tap, "event", event)
        monkeypatch.setattr(tap, "enabled", lambda: True)

    def of(self, stage: str) -> list[dict[str, Any]]:
        return [f for s, f in self.events if s == stage]


async def _compiled(eng: Engine) -> list[Any]:
    assert eng._graph is not None
    return [
        e
        for e in await eng._graph.edge_list(NS)
        if e.properties.get("compiled") == "edge_compiler_v2" and e.weight > 0
    ]


# ------------------------------------------------------------------ RM-92: qualifiers on edges


async def test_compiled_edges_carry_typed_qualifiers() -> None:
    eng = await _engine()
    try:
        ids = await _build(eng)
        edges = await _compiled(eng)
    finally:
        await eng.stop()
    assert edges
    by_record: dict[str, list[EdgeQualifiers]] = {}
    for edge in edges:
        q = edge_qualifiers(edge.properties)
        assert q is not None, edge.properties  # every compiled edge is qualified
        by_record.setdefault(str(edge.properties["record_id"]), []).append(q)
    asserted = by_record[ids["f_ilse"]]
    assert all(q.assertion_modality is AssertionModality.ASSERTED for q in asserted)
    assert all(q.erasure_state is ErasureState.ACTIVE and q.scope == NS for q in asserted)
    assert all(q.claim_state is not None and q.polarity is not None for q in asserted)
    assert all(q.known_at is not None and q.known_at.utcoffset() is not None for q in asserted)
    hypothetical = by_record[ids["f_dana_i"]]
    assert all(q.assertion_modality is AssertionModality.HYPOTHETICAL for q in hypothetical)


async def test_reprojection_with_qualifiers_is_idempotent() -> None:
    eng = await _engine()
    try:
        await _build(eng)
        again = await compile_graph_edges(eng._pipeline_ctx())
    finally:
        await eng.stop()
    # stored qualifiers round-trip exactly, so a second projection appends no LINK event
    assert again["status"] == "ok" and "written" not in again and again["unchanged"] > 0


# ------------------------------------------------------------------ RM-93: search uses the filter


def _served(spy: _Spy) -> dict[str, Any]:
    return spy.of("read.graph_filter")[0]


async def test_admitting_filter_keeps_the_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        flt = QualifierFilter(
            assertion_modalities=frozenset({AssertionModality.ASSERTED}),
            known_as_of=datetime.now(UTC) + timedelta(days=1),
        )
        await eng.search(QUERY, namespace=NS, top_k=4, graph_filter=flt)
    finally:
        await eng.stop()
    seen = _served(spy)
    assert 0 < seen["admitted"] < seen["edges"]  # hypothetical edges filtered out by type
    hop = spy.of("read.graph_hop")
    assert hop and hop[0]["convergent"] == [_node("Bram")]
    assert hop[0]["dropped_tentative"] == 0  # tentative edges never reached the walk


async def test_known_as_of_before_ingest_admits_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        flt = QualifierFilter(known_as_of=datetime(2000, 1, 1, tzinfo=UTC))
        hits = await eng.search(QUERY, namespace=NS, top_k=4, graph_filter=flt)
    finally:
        await eng.stop()
    assert _served(spy)["admitted"] == 0
    assert hits  # non-graph legs are unaffected by a graph filter


async def test_unknown_qualifier_never_passes(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        flt = QualifierFilter(topical_domains=frozenset({"health"}))  # no edge knows a domain
        await eng.search(QUERY, namespace=NS, top_k=4, graph_filter=flt)
    finally:
        await eng.stop()
    assert _served(spy)["admitted"] == 0
    assert spy.of("read.graph_hop") == []  # nothing left to walk: the leg is empty


async def test_no_filter_is_unchanged(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        await eng.search(QUERY, namespace=NS, top_k=4)
    finally:
        await eng.stop()
    assert spy.of("read.graph_filter") == []
    hop = spy.of("read.graph_hop")
    assert hop and hop[0]["convergent"] == [_node("Bram")] and hop[0]["dropped_tentative"] > 0


async def test_unqualified_graph_paths_are_skipped_under_a_filter() -> None:
    eng = await _engine(graph_leg=True)
    try:
        await _build(eng)
        free = await pipeline_legs._graph_walk(eng, NS, QUERY)
        with graph_filter_scope(QualifierFilter(known_as_of=datetime.now(UTC))):
            assert active_graph_filter() is not None
            blocked = await pipeline_legs._graph_walk(eng, NS, QUERY)
            entity = await pipeline_legs._entity_leg_v2(eng, NS, QUERY, [], 10)
    finally:
        await eng.stop()
    assert free  # the association walk reaches records without a filter
    assert blocked == [] and entity == []
    assert active_graph_filter() is None  # the scope is reset after the block


def test_empty_filter_binds_no_scope() -> None:
    with graph_filter_scope(QualifierFilter()):
        assert active_graph_filter() is None


async def test_read_cache_key_includes_the_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    seen: list[tuple[Any, ...]] = []
    real = read_request._cache_get

    async def spy(eng_: Any, query: str, ns: str, viewer: Any, sid: Any, params: Any) -> Any:
        seen.append(tuple(params))
        return await real(eng_, query, ns, viewer, sid, params)

    monkeypatch.setattr(read_request, "_cache_get", spy)
    flt = QualifierFilter(topical_domains=frozenset({"work"}))
    try:
        await _build(eng)
        await eng.read(QUERY, namespace=NS)
        await eng.read(QUERY, namespace=NS, graph_filter=flt)
    finally:
        await eng.stop()
    assert seen[0][-1] is None and seen[1][-1] == flt.model_dump_json()


# ------------------------------------------------------------------ RM-82 / RM-83 guards


def test_naive_times_are_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        EdgeQualifiers(valid_from=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        QualifierFilter(valid_at=datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        QualifierFilter(valid_window=(datetime(2026, 1, 1), datetime(2026, 2, 1)))


def test_erased_edges_never_pass_any_filter() -> None:
    for state in (ErasureState.ERASED, ErasureState.ERASURE_PENDING):
        q = EdgeQualifiers(erasure_state=state, topical_domain="health")
        assert not q.traversable
        assert not QualifierFilter().matches(q)
        assert not QualifierFilter(topical_domains=frozenset({"health"})).matches(q)
    active = EdgeQualifiers(erasure_state=ErasureState.ACTIVE, topical_domain="health")
    assert QualifierFilter(topical_domains=frozenset({"health"})).matches(active)
