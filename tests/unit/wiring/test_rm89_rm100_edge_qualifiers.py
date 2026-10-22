"""RM-85/89/90/92/100/102: the edge compiler fills claim, evidence, trust, binding, origin,
lifecycle and vocabulary qualifiers; evidence erasure contests and tombstones edges; existing
stores are backfilled by the sleep cycle and by ``memspine graph requalify``. Model-free."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from memspine import Engine
from memspine.derive.pipeline.stages.edge_compiler_v2 import (
    COMPILED,
    EdgeCompiler,
    compile_graph_edges,
    requalify_namespace,
    source_origin_for,
)
from memspine.services.graph.qualifiers import (
    QUALIFIERS_PROP,
    ClaimState,
    QualifierFilter,
    edge_qualifiers,
)
from memspine.store.records import MemoryRecord, SourceInfo

NS = "acme/alice"


def _engine(path: str = ":memory:") -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": path, "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"record_access": False},
        graph={"edge_compiler_v2": True},
    )


async def _turn(eng: Engine, text: str) -> str:
    rec = await eng.write(text, namespace=NS, memory_type="episodic")
    return rec.record_id


async def _fact(
    eng: Engine, src: str, rel: str, dst: str, *, parents: list[str], tags: list[str] = ()
) -> MemoryRecord:
    return await eng.write(
        f"{src} {rel} {dst}",
        namespace=NS,
        memory_type="semantic",
        entity=src,
        tags=["kind:state", f"rel:{rel}", f"dst:{dst}", *tags],
        source=SourceInfo(role="derived", channel="extract_graph", parents=parents),
    )


async def _compiled(eng: Engine, *, live: bool = True) -> list[Any]:
    assert eng._graph is not None
    return [
        e
        for e in await eng._graph.edge_list(NS)
        if e.properties.get("compiled") == COMPILED and (e.weight > 0 or not live)
    ]


def _of(edges: list[Any], record_id: str) -> list[Any]:
    return [e for e in edges if e.properties.get("record_id") == record_id]


# ------------------------------------------------------------------ RM-85: origin map


@pytest.mark.parametrize(
    ("klass", "channel", "origin"),
    [
        ("user_stated", "message", "raw_message"),
        ("assistant_proposed", "message", "raw_message"),
        ("document", "ingest", "document"),
        ("document", "rest", "document"),
        ("derived", "reflection", "reflection"),
        ("derived", "observation", "agent_observation"),
        ("user_stated", "agent_tool", "tool_output"),
        ("derived", "extract_graph", None),  # unmapped stays unknown
        ("user_stated", "internal", None),
        ("tool", "rest", None),
        ("external", "message", None),
    ],
)
def test_origin_map(klass: str, channel: str, origin: str | None) -> None:
    assert source_origin_for(klass, channel) == origin


def test_compiler_fills_origin_from_the_record() -> None:
    rec = MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content="Alice works at Acme",
        entity="Alice",
        tags=["kind:state", "rel:employed_by", "dst:Acme"],
        source=SourceInfo(role="user", channel="message", message_id="m-1"),
    )
    compiled = EdgeCompiler().compile([rec], NS)
    assert compiled.meta[rec.record_id]["origin"] == "raw_message"


# ------------------------------------------------------------------ RM-92: compiler fills qualifiers


async def test_compiled_edges_carry_the_new_qualifiers() -> None:
    eng = _engine()
    await eng.start()
    try:
        t1 = await _turn(eng, "Alice joined Acme in March")
        t2 = await _turn(eng, "Acme confirmed that Alice works for them")
        known = await _fact(eng, "Alice", "employed_by", "Acme", parents=[t1, t2])
        planned = await _fact(
            eng, "Alice", "works_on", "Apollo", parents=[t1], tags=["lifecycle:in_progress"]
        )
        done = await _fact(
            eng,
            "Alice",
            "has_goal",
            "Marathon",
            parents=[t2],
            tags=["lifecycle:closed", "outcome:done"],
        )
        bad = await _fact(  # outcome without closed status: both unknown
            eng, "Alice", "likes", "Chess", parents=[t2], tags=["lifecycle:open", "outcome:done"]
        )
        odd = await _fact(eng, "Alice", "zz_custom_rel", "Bob", parents=[t1])
        partly = await _fact(eng, "Alice", "likes", "it", parents=[t1])  # object rejected
        assert (await compile_graph_edges(eng._pipeline_ctx()))["status"] == "ok"
        edges = await _compiled(eng)
    finally:
        await eng.stop()

    def quals(rec: MemoryRecord) -> list[Any]:
        found = [edge_qualifiers(e.properties) for e in _of(edges, rec.record_id)]
        assert found and all(q is not None for q in found)
        return found

    for q in quals(known):
        assert q.claim_id == known.record_id
        assert q.evidence_ids == tuple(sorted([t1, t2]))
        assert q.independent_support_count == 2  # two turns, two lineage roots
        assert q.provenance is None or q.provenance.lineage_root is None
        assert q.trust == pytest.approx(known.trust) and q.trust_tier is not None
        assert q.binding_status == "bound"
        assert q.vocabulary_pack == "memspine.core" and q.vocabulary_version == "2"
        assert q.lifecycle_status is None and q.lifecycle_outcome is None
    for q in quals(planned):
        assert q.lifecycle_status == "in_progress" and q.lifecycle_outcome is None
        assert q.independent_support_count == 1 and q.provenance is not None
        assert q.provenance.lineage_root == t1
    for q in quals(done):
        assert (q.lifecycle_status, q.lifecycle_outcome) == ("closed", "done")
    for q in quals(bad):
        assert q.lifecycle_status is None and q.lifecycle_outcome is None
    for q in quals(odd):
        assert q.vocabulary_pack is None and q.vocabulary_version is None
    assert {q.binding_status for q in quals(partly)} == {"partly"}


async def test_unresolvable_evidence_leaves_support_unknown() -> None:
    eng = _engine()
    await eng.start()
    try:
        rec = await _fact(eng, "Alice", "employed_by", "Acme", parents=["no-such-record"])
        await compile_graph_edges(eng._pipeline_ctx())
        edges = _of(await _compiled(eng), rec.record_id)
    finally:
        await eng.stop()
    assert edges
    for e in edges:
        q = edge_qualifiers(e.properties)
        assert q is not None and q.evidence_ids == ("no-such-record",)
        assert q.independent_support_count is None
        assert not QualifierFilter(min_independent_support=0).matches(q)


# ------------------------------------------------------------------ RM-89: evidence erasure


async def test_evidence_erasure_contests_then_tombstones_and_survives_replay() -> None:
    eng = _engine()
    await eng.start()
    try:
        t1 = await _turn(eng, "Alice joined Acme in March")
        t2 = await _turn(eng, "Acme confirmed that Alice works for them")
        other = await _turn(eng, "Alice lives in Utrecht")
        claim = await _fact(eng, "Alice", "employed_by", "Acme", parents=[t1, t2])
        keep = await _fact(eng, "Alice", "resides_in", "Utrecht", parents=[other])
        await compile_graph_edges(eng._pipeline_ctx())
        assert _of(await _compiled(eng), claim.record_id)

        # one of two evidence turns erased: the id goes, the claim is contested, edges stay live
        await eng.forget(t1, namespace=NS)
        live = _of(await _compiled(eng), claim.record_id)
        assert live
        for e in live:
            q = edge_qualifiers(e.properties)
            assert q is not None
            assert q.claim_state is ClaimState.CONTESTED
            assert q.evidence_ids == (t2,) and t1 not in e.properties["evidence_ids"]
            assert q.independent_support_count == 1
            assert e.properties["contested"] is True
            assert e.properties["evidence_erased"] == [t1]
        assert all(  # an unrelated claim is untouched
            edge_qualifiers(e.properties).claim_state is ClaimState.CURRENT  # type: ignore[union-attr]
            for e in _of(await _compiled(eng), keep.record_id)
        )
        # the next projection keeps the erasure (no restore, nothing rewritten)
        again = await compile_graph_edges(eng._pipeline_ctx())
        assert again.get("written", 0) == 0, again
        assert all(
            t1 not in e.properties["evidence_ids"]
            for e in _of(await _compiled(eng), claim.record_id)
        )

        # the last evidence erased: the edges are tombstoned, and stay so
        await eng.forget(t2, namespace=NS)
        assert _of(await _compiled(eng), claim.record_id) == []
        dead = _of(await _compiled(eng, live=False), claim.record_id)
        assert dead and all(e.weight == 0 for e in dead)
        assert all(e.properties["evidence_erased"] == sorted([t1, t2]) for e in dead)
        assert all(e.properties["evidence_ids"] == [] for e in dead)
        third = await compile_graph_edges(eng._pipeline_ctx())
        assert third.get("written", 0) == 0, third
        assert _of(await _compiled(eng), claim.record_id) == []

        # replay of the log rebuilds the same graph
        before = {
            (e.src, e.dst, e.rel_type): (e.weight, e.properties)
            for e in await _compiled(eng, live=False)
        }
        await eng.rebuild()
        after = {
            (e.src, e.dst, e.rel_type): (e.weight, e.properties)
            for e in await _compiled(eng, live=False)
        }
        assert after == before
        assert _of(await _compiled(eng), keep.record_id)
    finally:
        await eng.stop()


# ------------------------------------------------------------------ RM-100: backfill


async def _strip_qualifiers(eng: Engine) -> int:
    assert eng._graph is not None
    edges = await _compiled(eng)
    for e in edges:
        props = {k: v for k, v in e.properties.items() if k != QUALIFIERS_PROP}
        await eng._graph.upsert_edge(e.src, e.dst, e.rel_type, props, namespace=NS)
    return len(edges)


async def test_sleep_cycle_backfills_edges_without_qualifiers() -> None:
    eng = _engine()
    await eng.start()
    try:
        t1 = await _turn(eng, "Alice joined Acme in March")
        await _fact(eng, "Alice", "employed_by", "Acme", parents=[t1])
        await compile_graph_edges(eng._pipeline_ctx())
        stripped = await _strip_qualifiers(eng)
        assert stripped and all(edge_qualifiers(e.properties) is None for e in await _compiled(eng))
        stats = await compile_graph_edges(eng._pipeline_ctx())
        assert stats["written"] == stripped
        assert all(edge_qualifiers(e.properties) is not None for e in await _compiled(eng))
        assert (await compile_graph_edges(eng._pipeline_ctx())).get("written", 0) == 0
    finally:
        await eng.stop()


async def test_requalify_dry_run_then_apply() -> None:
    eng = _engine()
    await eng.start()
    try:
        t1 = await _turn(eng, "Alice joined Acme in March")
        await _fact(eng, "Alice", "employed_by", "Acme", parents=[t1])
        await compile_graph_edges(eng._pipeline_ctx())
        stripped = await _strip_qualifiers(eng)
        ctx = eng._pipeline_ctx()
        dry = await requalify_namespace(ctx, NS, dry_run=True)
        assert dry["edges"] == stripped and dry["missing_qualifiers"] == stripped
        assert dry["to_update"] == stripped and "updated" not in dry
        assert all(edge_qualifiers(e.properties) is None for e in await _compiled(eng))
        done = await requalify_namespace(ctx, NS)
        assert done["updated"] == stripped
        assert all(edge_qualifiers(e.properties) is not None for e in await _compiled(eng))
        again = await requalify_namespace(ctx, NS)
        assert again["to_update"] == 0 and again["updated"] == 0
        assert again["missing_qualifiers"] == 0
    finally:
        await eng.stop()


async def test_requalify_refuses_without_the_compiled_layer() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
    )
    await eng.start()
    try:
        with pytest.raises(ValueError, match="edge_compiler_v2 is off"):
            await requalify_namespace(eng._pipeline_ctx(), NS, dry_run=True)
    finally:
        await eng.stop()


async def _seed_db(path: Path) -> int:
    eng = _engine(str(path))
    await eng.start()
    try:
        t1 = await _turn(eng, "Alice joined Acme in March")
        await _fact(eng, "Alice", "employed_by", "Acme", parents=[t1])
        await compile_graph_edges(eng._pipeline_ctx())
        return await _strip_qualifiers(eng)
    finally:
        await eng.stop()


def test_cli_graph_requalify(tmp_path: Path) -> None:
    import asyncio

    from memspine.cli import app

    db = tmp_path / "m.db"
    stripped = asyncio.run(_seed_db(db))
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        yaml.safe_dump(
            {
                "memories": {
                    "semantic": {"enabled": True},
                    "episodic": {"enabled": True},
                    "associative": {"enabled": True, "policies": {"entity_nodes": True}},
                },
                "graph": {"edge_compiler_v2": True},
            }
        ),
        encoding="utf-8",
    )
    runner = CliRunner()
    args = ["graph", "requalify", "--namespace", NS, "--db", str(db), "-t", "core", "-c", str(cfg)]
    dry = runner.invoke(app, [*args, "--dry-run"])
    assert dry.exit_code == 0, dry.output
    report = yaml.safe_load(dry.output)
    assert report["dry_run"] is True and report["missing_qualifiers"] == stripped
    assert report["to_update"] == stripped
    real = runner.invoke(app, args)
    assert real.exit_code == 0, real.output
    assert yaml.safe_load(real.output)["updated"] == stripped
    again = runner.invoke(app, args)
    assert yaml.safe_load(again.output)["missing_qualifiers"] == 0


def test_cli_graph_requalify_reports_a_missing_layer(tmp_path: Path) -> None:
    from memspine.cli import app

    result = CliRunner().invoke(
        app,
        ["graph", "requalify", "--namespace", NS, "--db", str(tmp_path / "x.db"), "-t", "core"],
    )
    assert result.exit_code == 1
    assert "edge_compiler_v2 is off" in result.output
