"""RM-88 caller visibility (both layers) and RM-91 compiled topical domains on the read path.

Model-free. ``a`` is the grantor namespace, ``b`` the grantee. A filtered search admits a
compiled edge only when its scope is the caller's own or one the caller holds an active
grant on, AND its source record passes the read's viewer check.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.derive.pipeline.stages.edge_compiler_v2 import compile_graph_edges
from memspine.observability import tap
from memspine.read.pipeline import legs as pipeline_legs
from memspine.services.graph.qualifiers import (
    EdgeQualifiers,
    ErasureState,
    QualifierFilter,
    edge_qualifiers,
    with_qualifiers,
)
from memspine.store.records import SourceInfo

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
QUERY = "How are Ilse and Tomasz connected"


@pytest.fixture(autouse=True)
def _namespace_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests read the ``read.graph_filter`` counts over the namespace's edges (the
    rows they plant do not touch the query's seeds), so they pin the leg to the namespace
    scan. The seed-scoped fetch (RM-104) runs the same caller check
    (``_caller_visible_edges``); its own visibility tests are in test_rm104_graph_leg_frontier."""
    monkeypatch.setattr(pipeline_legs, "_frontier_capable", lambda graph: False)


def _flt(**kw: Any) -> QualifierFilter:
    return QualifierFilter(known_as_of=datetime.now(UTC) + timedelta(days=1), **kw)


async def _engine() -> Engine:
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
            "shared": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        graph={"edge_compiler_v2": True},
        read={
            "hybrid": True,
            "record_access": False,
            "legs_v2": True,
            "graph_leg_v2": True,
            "graph_leg_seed_hits": 0,
        },
    )
    await eng.start()
    return eng


async def _fact(
    eng: Engine, ns: str, src: str, rel: str, dst: str, turn: str, *tags: str, **kw: Any
) -> str:
    parent = await eng.write(turn, namespace=ns, memory_type="episodic", valid_from=T0, **kw)
    rec = await eng.write(
        f"{src} {rel} {dst}",
        namespace=ns,
        memory_type="semantic",
        entity=src,
        tags=["kind:event", f"rel:{rel}", f"dst:{dst}", *tags],
        source=SourceInfo(role="derived", channel="extract_graph", parents=[parent.record_id]),
        valid_from=T0,
        **kw,
    )
    return rec.record_id


async def _build(eng: Engine, ns: str = "a", **kw: Any) -> dict[str, str]:
    ids = {
        "f_ilse": await _fact(
            eng, ns, "Ilse", "knows", "Bram", "Ilse introduced Bram to the chess club", **kw
        ),
        "f_tom": await _fact(
            eng, ns, "Tomasz", "employs", "Bram", "Tomasz hired Bram as a carpenter", **kw
        ),
    }
    stats = await compile_graph_edges(eng._pipeline_ctx())
    assert stats["status"] in ("ok", "partial")
    return ids


class _Spy:
    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

        def event(stage: str, /, **fields: Any) -> None:
            self.events.append((stage, fields))

        monkeypatch.setattr(tap, "event", event)
        monkeypatch.setattr(tap, "enabled", lambda: True)

    def last(self) -> dict[str, Any]:
        found = [f for s, f in self.events if s == "read.graph_filter"]
        assert found, "the filtered graph leg did not run"
        self.events.clear()
        return found[-1]


async def _put_edge(eng: Engine, ns: str, src: str, dst: str, scope: str, record_id: str) -> None:
    """A compiled-layer edge in ``ns``'s graph with qualifier ``scope`` (a shared-graph row)."""
    assert eng._graph is not None
    q = EdgeQualifiers(
        scope=scope,
        known_at=datetime.now(UTC),
        erasure_state=ErasureState.ACTIVE,
    )
    props = with_qualifiers(
        {"compiled": "edge_compiler_v2", "record_id": record_id, "weight": 1.0}, q
    )
    await eng._graph.upsert_edge(src, dst, "same_as", props, namespace=ns)


# ------------------------------------------------------------------ RM-88


async def test_own_scope_edges_pass(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=_flt())
    finally:
        await eng.stop()
    seen = spy.last()
    assert seen["admitted"] > 0 and seen["caller_hidden"] == 0


async def test_f13_grantee_merge_is_never_visible_to_the_grantor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RM-55/F13: a grantee-only identity merge carries the grantee scope; even where the
    grantor's graph holds the row, the grantor (who holds no grant from the grantee) never
    admits it, while the grantee does."""
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng, "a")
        await eng.grant("b", namespace="a")  # b may read a; NOT the other direction
        evidence = await eng.write(
            "Bram from a's notes is my cousin Bram", namespace="b", memory_type="semantic"
        )
        # the merge row is present in both graphs (distinct keys: edges are keyed globally)
        await _put_edge(eng, "a", "ent:a:bram", "ent:b:bram", "b", evidence.record_id)
        await _put_edge(eng, "b", "ent:b:bram", "ent:a:bram", "b", evidence.record_id)
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=_flt())
        grantor = spy.last()
        await eng.search("Bram cousin", namespace="b", top_k=4, graph_filter=_flt())
        grantee = spy.last()
    finally:
        await eng.stop()
    assert grantor["caller_hidden"] == 1  # the grantee's merge never reaches the grantor
    assert grantee["caller_hidden"] == 0 and grantee["admitted"] == 1


async def test_grant_admits_and_revocation_hides_at_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        ids = await _build(eng, "a")
        # a row scoped to a, sourced from a's record, present in b's graph
        await _put_edge(eng, "b", "ent:b:ilse", "ent:a:bram", "a", ids["f_ilse"])
        await eng.write("Ilse plays chess", namespace="b", memory_type="semantic")
        await eng.search(QUERY, namespace="b", top_k=4, graph_filter=_flt())
        before = spy.last()
        await eng.grant("b", namespace="a")
        await eng.search(QUERY, namespace="b", top_k=4, graph_filter=_flt())
        granted = spy.last()
        await eng.revoke("b", namespace="a")
        await eng.search(QUERY, namespace="b", top_k=4, graph_filter=_flt())
        revoked = spy.last()
    finally:
        await eng.stop()
    assert before["caller_hidden"] == 1 and before["admitted"] == 0
    assert granted["caller_hidden"] == 0 and granted["admitted"] == 1
    assert revoked["caller_hidden"] == 1 and revoked["admitted"] == 0


async def test_record_viewer_layer_hides_edges(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        private = await _fact(
            eng,
            "a",
            "Ilse",
            "knows",
            "Bram",
            "Ilse introduced Bram to the chess club",
            participants=["Ilse", "Tomasz"],
            visibility="participants",
        )
        await _fact(eng, "a", "Tomasz", "employs", "Bram", "Tomasz hired Bram as a carpenter")
        await compile_graph_edges(eng._pipeline_ctx())
        assert eng._graph is not None
        private_edges = sum(
            1
            for e in await eng._graph.edge_list("a")
            if e.properties.get("record_id") == private and e.weight > 0
        )
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=_flt(), viewer="Ilse")
        member = spy.last()
        await eng.search(QUERY, namespace="a", top_k=4, graph_filter=_flt(), viewer="Dana")
        outsider = spy.last()
    finally:
        await eng.stop()
    assert private_edges > 0
    assert member["caller_hidden"] == 0 and member["admitted"] > private_edges
    assert outsider["caller_hidden"] == private_edges  # the record viewer layer hides them
    assert outsider["admitted"] == member["admitted"] - private_edges


async def test_no_filter_applies_no_caller_layer(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        await _build(eng)
        await eng.search(QUERY, namespace="a", top_k=4)
    finally:
        await eng.stop()
    assert [f for s, f in spy.events if s == "read.graph_filter"] == []


# ------------------------------------------------------------------ RM-91


async def test_topic_tagger_runs_only_when_bound() -> None:
    """(d) GLiNER topic tags: an attached stub tagger is inert without the binding; with
    ``models.bindings.gliner_topic`` it tags writes. Callers cannot forge the tag."""
    calls: list[str] = []

    def stub(text: str) -> list[str]:
        calls.append(text)
        return ["health/condition"]

    plain = await _engine()
    try:
        plain._topic_tagger = stub  # type: ignore[attr-defined]
        rec = await plain.write(
            "I have asthma", namespace="a", tags=["gliner_topic:forged", "domain:health"]
        )
    finally:
        await plain.stop()
    assert calls == []
    assert "gliner_topic:forged" not in rec.tags and "domain:health" in rec.tags

    bound = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        embedding={"provider": "hash", "settings": {}},
        models={
            "profiles": {"topics": {"kind": "gliner", "provider": "gliner2"}},
            "bindings": {"gliner_topic": "topics"},
        },
    )
    await bound.start()
    try:
        bound._topic_tagger = stub  # type: ignore[attr-defined]
        rec = await bound.write("I have asthma", namespace="a")
    finally:
        await bound.stop()
    assert calls == ["I have asthma"]
    assert "gliner_topic:health/condition" in rec.tags


async def test_compiled_edges_carry_domain_and_source(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine()
    spy = _Spy(monkeypatch)
    try:
        ids = {
            "home": await _fact(eng, "a", "Ilse", "resides_in", "Utrecht", "Ilse lives in Utrecht"),
            "tagged": await _fact(
                eng, "a", "Tomasz", "knows", "Bram", "Tomasz knows Bram", "domain:work/team"
            ),
            "plain": await _fact(eng, "a", "Ilse", "knows", "Bram", "Ilse knows Bram"),
        }
        await compile_graph_edges(eng._pipeline_ctx())
        assert eng._graph is not None
        edges = [
            e
            for e in await eng._graph.edge_list("a")
            if e.properties.get("compiled") == "edge_compiler_v2" and e.weight > 0
        ]
        await eng.search(
            QUERY, namespace="a", top_k=4, graph_filter=_flt(topical_domains=frozenset({"work"}))
        )
        work = spy.last()
    finally:
        await eng.stop()
    by_record: dict[str, set[tuple[str | None, str | None]]] = {}
    for edge in edges:
        q = edge_qualifiers(edge.properties)
        assert q is not None
        by_record.setdefault(str(edge.properties["record_id"]), set()).add(
            (q.topical_domain, q.topical_domain_source)
        )
    assert by_record[ids["home"]] == {("location/residence", "predicate")}
    assert by_record[ids["tagged"]] == {("work/team", "ingest_tag")}
    assert by_record[ids["plain"]] == {(None, None)}  # nothing inferred
    tagged_edges = sum(1 for e in edges if e.properties["record_id"] == ids["tagged"])
    assert work["admitted"] == tagged_edges  # "work" admits its descendant "work/team" only
