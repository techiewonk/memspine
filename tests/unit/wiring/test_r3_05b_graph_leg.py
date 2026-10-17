"""R3-05b: the graph one-hop leg (read.graph_leg_v2, OneHopConvergent). Model-free.

Edges are built through the F-02c compiler from stub facts (no model, hash embedder).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.derive.pipeline.stages.edge_compiler_v2 import compile_graph_edges
from memspine.read import legs as lib
from memspine.read.legs import core as legs_core
from memspine.read.legs import graph as legs_graph
from memspine.read.pipeline import router
from memspine.store.layer_registry import LayerRegistry, LayerTemplate
from memspine.store.records import MemoryRecord, SourceInfo

NS = "a"
T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
QUERY = "How are Ilse and Tomasz connected"
TURNS = {
    "ilse": "Ilse introduced Bram to the chess club",
    "tom": "Tomasz hired Bram as a carpenter last spring",
    "noor": "Noor met Carlos at the market",
    "dana_i": "Ilse might invite Dana to the chess club",
    "dana_t": "Tomasz might hire Dana as an apprentice",
}


def _node(name: str) -> str:
    return f"ent:{NS}:{name.lower()}"


# ------------------------------------------------------------------ pure walk


def test_flag_defaults_off() -> None:
    cfg = ReadConfig()
    assert cfg.graph_leg_v2 is False
    assert cfg.graph_leg_min_seeds == 2 and cfg.graph_leg_seed_hits == 3


def test_leg_is_not_in_the_default_registry_but_in_the_extended_one() -> None:
    assert "graph" not in lib.REGISTRY.names()
    ext = lib.with_graph_leg(lib.REGISTRY)
    assert ext.names() == (*lib.REGISTRY.names(), "graph")
    assert ext.family_of("graph") == "structured"
    assert ext.get("graph").layers == ("L3",)
    assert lib.with_graph_leg(lib.REGISTRY) is ext
    assert "graph" not in lib.REGISTRY.names()


def test_one_hop_picks_the_bridging_node_and_drops_tentative() -> None:
    edges = [
        lib.HopEdge("A", "X", "r1"),
        lib.HopEdge("B", "X", "r2"),
        lib.HopEdge("A", "Y", "r3"),  # reached from A only
        lib.HopEdge("A", "Z", "r4", tentative=True),
        lib.HopEdge("B", "Z", "r5", tentative=True),
    ]
    res = lib.one_hop(["A", "B"], edges)
    assert res.convergent == ["X"] and res.dropped_tentative == 2
    assert set(res.scores) == {"r1", "r2"} and all(s == 1.0 for s in res.scores.values())
    one = lib.one_hop(["A", "B"], edges, min_seeds=1)
    assert set(one.convergent) == {"X", "Y"}  # tentative Z still excluded


def _ctx(records: list[MemoryRecord], **kw: Any) -> lib.LegContext:
    return lib.LegContext(query="q", namespace=NS, fetch_k=10, records=records, **kw)


def _fact(rid: str, layer_channel: str = "extract_graph") -> MemoryRecord:
    return MemoryRecord(
        record_id=rid,
        namespace=NS,
        content=rid,
        memory_type="semantic",
        valid_from=T0,
        source=SourceInfo(role="derived", channel=layer_channel, parents=[]),
    )


async def test_no_l3_gives_an_empty_leg_with_a_reason() -> None:
    # MF-07: readiness comes from compiled edges with a live supporting record, not from L3 records.
    edges = [lib.HopEdge("A", "X", "r1"), lib.HopEdge("B", "X", "r2")]
    out = await lib.OneHopConvergent().run(
        _ctx([_fact("r9", "mining")], graph_seeds=["A", "B"], graph_edges=edges)
    )
    assert out.hits == [] and out.empty_reason == "no_edges"  # no edge has a live supporting record
    reg = LayerRegistry.of(NS, LayerTemplate(), {"L0": 2, "L1": 1})
    out = await lib.OneHopConvergent().run(
        _ctx([_fact("r1")], graph_seeds=["A", "B"], graph_edges=[], layer_registry=reg)
    )
    assert out.empty_reason == "no_edges"
    # and an L3-less store whose edges are backed by live facts is NOT inert (the point of MF-07)
    out = await lib.OneHopConvergent().run(
        _ctx(
            [_fact("r1", "mining"), _fact("r2", "mining")],
            graph_seeds=["A", "B"],
            graph_edges=edges,
            layer_registry=reg,
        )
    )
    assert [h.record_id for h in out.hits] == ["r1", "r2"]


async def test_leg_reasons_and_hits() -> None:
    recs = [_fact("r1"), _fact("r2")]
    reg = LayerRegistry.of(NS, LayerTemplate(), {"L3": 2})
    edges = [lib.HopEdge("A", "X", "r1"), lib.HopEdge("B", "X", "r2")]
    leg = lib.OneHopConvergent()
    assert (await leg.run(_ctx(recs, layer_registry=reg, graph_edges=edges))).empty_reason == (
        "no_seeds"
    )
    assert (await leg.run(_ctx(recs, layer_registry=reg, graph_seeds=["A"]))).empty_reason == (
        "no_edges"
    )
    lone = await leg.run(_ctx(recs, layer_registry=reg, graph_seeds=["A", "C"], graph_edges=edges))
    assert lone.empty_reason == "no_convergence"
    out = await leg.run(_ctx(recs, layer_registry=reg, graph_seeds=["A", "B"], graph_edges=edges))
    assert [h.record_id for h in out.hits] == ["r1", "r2"]
    # a record that is not live is never returned: without r2's edge X is reached by A alone
    live_only = await leg.run(
        _ctx(recs[:1], layer_registry=reg, graph_seeds=["A", "B"], graph_edges=edges)
    )
    assert live_only.hits == [] and live_only.empty_reason == "no_convergence"


# ------------------------------------------------------------------ the wiring (engine, GC arm)


async def _engine(**read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': ":memory:", 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
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
            "graph_leg_seed_hits": 0,
            **read,
        },
    )
    await eng.start()
    return eng


async def _build(eng: Engine, *, tentative: bool = True, compile_: bool = True) -> dict[str, str]:
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
        assert rec.layer == "L3"
        ids[key] = rec.record_id

    await fact("f_ilse", "Ilse", "knows", "Bram", "ilse")
    await fact("f_tom", "Tomasz", "employs", "Bram", "tom")
    await fact("f_noor", "Noor", "knows", "Carlos", "noor")
    if tentative:
        await fact("f_dana_i", "Ilse", "may_invite", "Dana", "dana_i", "hypothetical")
        await fact("f_dana_t", "Tomasz", "may_hire", "Dana", "dana_t", "hypothetical")
    if compile_:
        stats = await compile_graph_edges(eng._pipeline_ctx())
        assert stats["status"] == "ok"
    return ids


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.fused: list[Any] = []

        def event(stage: str, /, **f: Any) -> None:
            self.events.append((stage, f))

        for mod in (legs_core, legs_graph):
            monkeypatch.setattr(mod.tap, "event", event)
        monkeypatch.setattr(legs_graph.tap, "enabled", lambda: True)
        real = router.fuse_families

        def spy(vh: Any, lh: Any, hybrid: Any, extra_legs: Any, **kw: Any) -> Any:
            self.fused.extend(extra_legs)
            return real(vh, lh, hybrid, extra_legs, **kw)

        monkeypatch.setattr(router, "fuse_families", spy)

    def of(self, stage: str) -> list[dict[str, Any]]:
        return [f for s, f in self.events if s == stage]

    def graph_leg(self) -> list[Any]:
        return [leg for leg in self.fused if getattr(leg, "name", "") == "graph"]


async def test_flag_off_never_runs_the_graph_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        await eng.search(QUERY, namespace=NS, top_k=4)
    finally:
        await eng.stop()
    assert "graph" not in [f["leg"] for f in spy.of("read.leg")]
    assert spy.of("read.graph_hop") == [] and spy.graph_leg() == []


async def test_no_l3_flag_on_is_an_empty_leg_not_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(graph_leg_v2=True)
    spy = _Spy(monkeypatch)
    try:
        await eng.write(TURNS["ilse"], namespace=NS, memory_type="episodic", valid_from=T0)
        hits = await eng.search(QUERY, namespace=NS, top_k=3)
    finally:
        await eng.stop()
    assert hits
    ev = next(f for f in spy.of("read.leg") if f["leg"] == "graph")
    # MF-07: no compiled edges -> "no_edges" (readiness is edges, not L3 records)
    assert ev["count"] == 0 and ev["empty_reason"] == "no_edges"
    assert ev["family"] == "structured" and ev["layers"] == ["L3"]
    assert spy.graph_leg() == []


async def test_convergence_picks_the_bridge_and_fusion_receives_the_leg(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = await _engine(graph_leg_v2=True, family_fusion=True)
    spy = _Spy(monkeypatch)
    try:
        ids = await _build(eng)
        await eng.search(QUERY, namespace=NS, top_k=4)
    finally:
        await eng.stop()
    hop = spy.of("read.graph_hop")
    assert hop and set(hop[0]["seeds"]) == {_node("Ilse"), _node("Tomasz")}
    assert hop[0]["convergent"] == [_node("Bram")]  # the bridging node, nothing else
    assert _node("Dana") not in hop[0]["reached"]  # tentative edges never reach it
    assert hop[0]["dropped_tentative"] > 0
    legs = spy.graph_leg()
    assert legs  # fusion received the leg: evidence turns of the bridging facts
    assert {h.record_id for h in legs[0]} == {ids["ilse"], ids["tom"]}
    ev = next(f for f in spy.of("read.leg") if f["leg"] == "graph")
    assert ev["count"] == 2 and ev["empty_reason"] is None


async def test_tentative_edges_do_not_bridge(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(graph_leg_v2=True)
    spy = _Spy(monkeypatch)
    try:
        ids = await _build(eng)
        await eng.write("Bram is a carpenter", namespace=NS, memory_type="episodic", valid_from=T0)
        await eng.search(QUERY, namespace=NS, top_k=4)
    finally:
        await eng.stop()
    legs = spy.graph_leg()
    got = {h.record_id for h in legs[0]} if legs else set()
    assert ids["dana_i"] not in got and ids["dana_t"] not in got
    assert ids["noor"] not in got


async def test_erased_evidence_is_not_returned(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(graph_leg_v2=True)
    spy = _Spy(monkeypatch)
    try:
        ids = await _build(eng)
        await eng.forget(ids["tom"], namespace=NS, hard=True, cascade=False)
        hits = await eng.search(QUERY, namespace=NS, top_k=6)
    finally:
        await eng.stop()
    legs = spy.graph_leg()
    served = {h.record_id for leg in legs for h in leg}
    assert ids["tom"] not in served
    assert ids["tom"] not in {r.record_id for r, _ in hits}


async def test_consent_gate_hides_lifted_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(graph_leg_v2=True)
    spy = _Spy(monkeypatch)
    try:
        ids = await _build(eng)
        monkeypatch.setattr(eng, "_consent_ok", lambda r: r.record_id != ids["ilse"])
        await eng.search(QUERY, namespace=NS, top_k=6)
    finally:
        await eng.stop()
    served = {h.record_id for leg in spy.graph_leg() for h in leg}
    assert ids["ilse"] not in served


async def test_seed_hits_add_entities(monkeypatch: pytest.MonkeyPatch) -> None:
    # the query names only Ilse; the top base hit is Tomasz's turn, whose fact entity seeds the walk
    eng = await _engine(graph_leg_v2=True, graph_leg_seed_hits=5)
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        await eng.search("Tomasz hired Bram; who else knows Ilse", namespace=NS, top_k=4)
    finally:
        await eng.stop()
    hop = spy.of("read.graph_hop")
    assert hop and _node("Ilse") in hop[0]["seeds"]
    assert len(hop[0]["seeds"]) >= 2
