"""F-02c: edge compiler and link projection (``graph.edge_compiler``), model-free."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import GraphConfig
from memspine.config.schema.observability import TapConfig
from memspine.derive.pipeline.stages.edge_compiler import (
    COMPILED,
    EdgeCompiler,
    LinkProjection,
    compile_graph_edges,
)
from memspine.memories.associative.entities import entity_node_id
from memspine.observability import tap
from memspine.store.records import MemoryRecord
from memspine.workers.schedule import COMPILE_EDGES_STAGE, SLEEP_CYCLE_ORDER

NS = "acme/alice"


@pytest.fixture(autouse=True)
def _tap_reset():
    tap.shutdown()
    yield
    tap.shutdown()


def _engine(*, on: bool = True, **extra: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False},
        graph={"edge_compiler": on},
        **extra,
    )


async def _fact(
    eng: Engine,
    src: str,
    rel: str,
    dst: str,
    *,
    ns: str = NS,
    tags: list[str] | None = None,
    kind: str = "event",
) -> MemoryRecord:
    return await eng.write(
        f"{src} {rel} {dst}",
        namespace=ns,
        memory_type="semantic",
        entity=src,
        tags=[f"kind:{kind}", f"rel:{rel}", f"dst:{dst}", *(tags or [])],
    )


async def _run(eng: Engine) -> dict[str, object]:
    return await compile_graph_edges(eng._pipeline_ctx())


async def _compiled(eng: Engine, ns: str = NS):
    assert eng._graph is not None
    return [e for e in await eng._graph.edge_list(ns) if e.properties.get("compiled") == COMPILED]


def _projection(edges):
    return [e for e in edges if e.properties.get("edge_kind") == "projection"]


def test_flag_default_off_and_stage_not_scheduled() -> None:
    assert GraphConfig().edge_compiler is False
    assert COMPILE_EDGES_STAGE not in SLEEP_CYCLE_ORDER


async def test_flag_off_stage_skips_and_graph_unchanged() -> None:
    eng = _engine(on=False)
    await eng.start()
    try:
        await _fact(eng, "Alice", "works_at", "Acme")
        before = await eng._graph.edge_count()
        stats = await _run(eng)
        assert stats["status"] == "skipped"
        assert await eng._graph.edge_count() == before
        assert await _compiled(eng) == []
        assert COMPILE_EDGES_STAGE not in await eng.sleep()
    finally:
        await eng.stop()


async def test_compiles_assertions_to_typed_edges() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await _fact(eng, "Alice", "works_at", "Acme")
        stats = await _run(eng)
        assert stats["status"] == "ok" and stats["written"] >= 2
        edges = await _compiled(eng)
        alice, acme = entity_node_id(NS, "alice"), entity_node_id(NS, "acme")
        proj = _projection(edges)
        assert [(e.src, e.dst, e.rel_type) for e in proj] == [(alice, acme, "works_at")]
        p = proj[0].properties
        assert p["record_id"] == rec.record_id and p["tentative"] is False
        assert p["valid_from"] and p["source_at"] and p["event_precision"]
        assert p["evidence_ids"] and p["trust"] == pytest.approx(rec.trust)
        assert "Acme" not in str(p) and "works_at" not in str(p["evidence_ids"])  # ids, never text
        roles = {e.rel_type for e in edges if e.properties.get("edge_kind") == "role"}
        assert {"role:subject", "role:object"} <= roles
        # the assertion node is labelled and owned by the compiler
        node_ids = {e.src for e in edges if e.rel_type == "role:subject"}
        assert len(node_ids) == 1
    finally:
        await eng.stop()


async def test_reprojection_is_idempotent() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fact(eng, "Alice", "works_at", "Acme")
        await _fact(eng, "Alice", "lives_in", "Utrecht")
        first = await _run(eng)
        count = await eng._graph.edge_count()
        before = await _compiled(eng)
        second = await _run(eng)
        assert first["written"] > 0
        assert second.get("written", 0) == 0 and second["unchanged"] == first["written"]
        assert await eng._graph.edge_count() == count
        after = await _compiled(eng)
        key = lambda e: (e.src, e.dst, e.rel_type)
        assert sorted(map(key, before)) == sorted(map(key, after))
    finally:
        await eng.stop()


async def test_tentative_edges_are_tagged_and_hidden_by_default() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fact(eng, "Alice", "works_at", "Acme")
        await _fact(eng, "Alice", "visits", "Paris", tags=["planned"])
        await _run(eng)
        edges = _projection(await _compiled(eng))
        by_rel = {e.rel_type: e for e in edges}
        assert by_rel["works_at"].properties["tentative"] is False
        assert by_rel["visits"].properties["tentative"] is True
        proj = LinkProjection(eng._graph, eng._append_and_project)
        shown = {
            e.rel_type
            for e in await proj.visible_edges(NS)
            if e.properties["edge_kind"] == "projection"
        }
        assert shown == {"works_at"}
        everything = {
            e.rel_type
            for e in await proj.visible_edges(NS, include_tentative=True)
            if e.properties["edge_kind"] == "projection"
        }
        assert everything == {"works_at", "visits"}
    finally:
        await eng.stop()


def test_compiler_is_pure_and_tags_unbound_and_assistant_content() -> None:
    comp = EdgeCompiler()
    good = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content="Alice works at Acme",
        entity="Alice",
        tags=["kind:state", "rel:works_at", "dst:Acme"],
    )
    junk = MemoryRecord(  # object is a pronoun: rejected as an entity -> partly bound -> tentative
        namespace=NS,
        memory_type="semantic",
        content="Alice likes it",
        entity="Alice",
        tags=["kind:event", "rel:likes", "dst:it"],
    )
    a = comp.compile([good, junk], NS)
    b = comp.compile([junk, good], NS)
    assert a.delta == b.delta  # order independent
    by_rec = {e.record_id: e.tentative for e in a.delta.edges}
    assert by_rec[good.record_id] is False and by_rec[junk.record_id] is True
    assert not [
        e for e in a.delta.edges if e.record_id == junk.record_id and e.kind == "projection"
    ]


async def test_edges_removed_when_the_supporting_assertion_is_erased() -> None:
    eng = _engine()
    await eng.start()
    try:
        keep = await _fact(eng, "Alice", "works_at", "Acme")
        gone = await _fact(eng, "Alice", "lives_in", "Utrecht")
        await _run(eng)
        assert {e.properties["record_id"] for e in await _compiled(eng)} == {
            keep.record_id,
            gone.record_id,
        }
        await eng.forget(gone.record_id, namespace=NS, hard=True)
        live = await _compiled(eng)
        assert {e.properties["record_id"] for e in live if e.weight > 0} == {keep.record_id}
        assert [e.rel_type for e in _projection([e for e in live if e.weight > 0])] == ["works_at"]
        # a later sweep does not bring them back
        await _run(eng)
        assert {e.properties["record_id"] for e in await _compiled(eng) if e.weight > 0} == {
            keep.record_id
        }
    finally:
        await eng.stop()


async def test_stage_retires_edges_of_a_record_that_is_no_longer_an_assertion(monkeypatch) -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fact(eng, "Alice", "advises", "Acme")  # keeps both entity nodes alive
        rec = await _fact(eng, "Alice", "works_at", "Acme")
        await _run(eng)
        assert await _compiled(eng)

        # soft forget with the erasure hook bypassed: the sweep alone retires the edges
        async def _noop(*_a: Any, **_k: Any) -> None:
            return None

        monkeypatch.setattr(
            "memspine.derive.pipeline.stages.edge_compiler.retract_after_forget", _noop
        )
        await eng.forget(rec.record_id, namespace=NS)
        mine = lambda: [
            e for e in await_edges if e.weight > 0 and e.properties["record_id"] == rec.record_id
        ]
        await_edges = await _compiled(eng)
        assert mine()
        stats = await _run(eng)
        assert stats["retracted_edges"] > 0
        await_edges = await _compiled(eng)
        assert not mine() and await_edges
    finally:
        await eng.stop()


async def test_scope_keyed_edges_use_tenant_and_owner() -> None:
    rec = MemoryRecord(
        namespace="acme/alice/s1",
        memory_type="semantic",
        content="Alice works at Acme",
        entity="Alice",
        tags=["kind:state", "rel:works_at", "dst:Acme"],
    )
    twin = rec.model_copy(update={"namespace": "acme/alice/s2", "record_id": rec.record_id})
    on = EdgeCompiler()
    envelope = on.envelope(rec)
    assert (envelope.scope.tenant, envelope.scope.owner) == ("acme", "alice/s1")
    other_tenant = rec.model_copy(update={"namespace": "globex/alice/s1"})
    k_on = {e.key for e in on.compile([rec], rec.namespace).delta.edges}
    k_other = {e.key for e in on.compile([other_tenant], other_tenant.namespace).delta.edges}
    assert k_on and k_on.isdisjoint(k_other)  # tenants never share a key
    assert twin.namespace != rec.namespace


async def test_stage_uses_typed_scope_and_is_scheduled_when_enabled() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fact(eng, "Alice", "works_at", "Acme")
        stats = await eng.sleep()
        names = list(stats)
        assert stats[COMPILE_EDGES_STAGE]["status"] == "ok"
        assert names.index(COMPILE_EDGES_STAGE) < names.index("summarize_entities")
        edges = await _compiled(eng)
        assert edges and all(e.properties["tenant"] == "acme" for e in edges)
        assert all(e.properties["owner"] == "alice" for e in edges)
    finally:
        await eng.stop()


async def test_tap_events_graph_compile() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    eng = _engine()
    await eng.start()
    try:
        rec = await _fact(eng, "Alice", "works_at", "Acme")
        with tap.request("job", stage=COMPILE_EDGES_STAGE):
            await _run(eng)
            await eng.forget(rec.record_id, namespace=NS, hard=True)
    finally:
        await eng.stop()
    events = {
        e["stage"]: e["fields"]
        for t in tap.memory_traces()
        for e in t["events"]
        if e["stage"].startswith("graph.compile.")
    }
    assert {"graph.compile.start", "graph.compile.delta", "graph.compile.project"} <= set(events)
    assert events["graph.compile.delta"]["edges"] >= 2
    assert events["graph.compile.project"]["written"] >= 2
    assert events["graph.compile.retract"]["reason"] == "erasure"
    assert not any(
        "field_rejected" in e["fields"] for t in tap.memory_traces() for e in t["events"]
    )
