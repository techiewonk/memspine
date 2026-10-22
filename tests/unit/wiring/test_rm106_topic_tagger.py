"""RM-106: the Engine builds the GLiNER topic tagger from the bound ``gliner_topic`` profile
and feeds it as the lowest-precedence domain source on the write path and the compile /
requalify path. Model-free: the tagger's model binding is replaced by a stub (no download)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from memspine import Engine
from memspine.derive.pipeline.stages.edge_compiler_v2 import (
    compile_graph_edges,
    requalify_namespace,
)
from memspine.derive.topics import GlinerTopicTagger
from memspine.services.graph.qualifiers import edge_qualifiers
from memspine.store.records import SourceInfo

NS = "a"
T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


class _Extractor:
    """Answers ``location residence`` for "moved" texts and ``cinema`` for "film" texts."""

    def __init__(self) -> None:
        self.texts: list[str] = []
        self.fail = False

    def classify_text(self, text: str, fields: dict[str, list[str]], **_: Any) -> Any:
        if self.fail:
            raise RuntimeError("model unavailable")
        self.texts.append(text)
        if "film" in text:
            return {"topic": {"label": "cinema", "confidence": 0.9}}
        if "chess" in text:
            return {"topic": {"label": "preferences", "confidence": 0.2}}  # below threshold
        return {"topic": {"label": "relationships", "confidence": 0.8}}


class _Binding:
    def __init__(self, extractor: _Extractor) -> None:
        self.extractor = extractor

    async def call(self, fn: Callable[[Any], Any]) -> Any:
        return fn(self.extractor)


async def _engine(*, bound: bool, **topic: Any) -> Engine:
    models: dict[str, Any] = {}
    if bound:
        models = {
            "profiles": {
                "topics": {
                    "kind": "gliner",
                    "provider": "gliner2",
                    "settings": {"tasks": ["classification"], "local_files_only": True},
                }
            },
            "bindings": {"gliner_topic": "topics"},
            "topic": {"labels": {"cinema": "media/film"}, **topic},
        }
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
        models=models,
    )
    await eng.start()
    return eng


def _stub(eng: Engine) -> _Extractor:
    """Swap the built tagger's lazy GLiNER binding for a stub (no model load)."""
    tagger = eng._topic_tagger
    assert isinstance(tagger, GlinerTopicTagger)
    extractor = _Extractor()
    tagger._model = _Binding(extractor)
    return extractor


async def _fact(eng: Engine, src: str, rel: str, dst: str, content: str, *tags: str) -> str:
    parent = await eng.write(content, namespace=NS, memory_type="episodic", valid_from=T0)
    rec = await eng.write(
        content,
        namespace=NS,
        memory_type="semantic",
        entity=src,
        tags=["kind:event", f"rel:{rel}", f"dst:{dst}", *tags],
        source=SourceInfo(role="derived", channel="extract_graph", parents=[parent.record_id]),
        valid_from=T0,
    )
    return rec.record_id


async def _domains(eng: Engine) -> dict[str, set[tuple[str | None, str | None]]]:
    assert eng._graph is not None
    out: dict[str, set[tuple[str | None, str | None]]] = {}
    for edge in await eng._graph.edge_list(NS):
        if edge.properties.get("compiled") != "edge_compiler_v2" or edge.weight <= 0:
            continue
        q = edge_qualifiers(edge.properties)
        assert q is not None
        out.setdefault(str(edge.properties["record_id"]), set()).add(
            (q.topical_domain, q.topical_domain_source)
        )
    return out


async def test_unbound_engine_is_inert() -> None:
    eng = await _engine(bound=False)
    try:
        assert eng._topic_tagger is None
        assert eng._pipeline_ctx().topic_tagger is None
        rid = await _fact(eng, "Ilse", "knows", "Bram", "Ilse and Bram watched a film")
        await compile_graph_edges(eng._pipeline_ctx())
        got = await _domains(eng)
    finally:
        await eng.stop()
    assert got[rid] == {(None, None)}


async def test_bound_tagger_is_built_offline_and_tags_writes() -> None:
    eng = await _engine(bound=True)
    try:
        tagger = eng._topic_tagger
        assert isinstance(tagger, GlinerTopicTagger)
        assert tagger.labels["cinema"] == "media/film"  # operator map
        assert tagger.labels["location residence"] == "location/residence"  # pack domain
        assert eng._pipeline_ctx().topic_tagger is tagger
        extractor = _stub(eng)
        rec = await eng.write("We watched a film", namespace=NS)
        extractor.fail = True  # a failing tagger never blocks ingestion
        after = await eng.write("We watched another film", namespace=NS)
    finally:
        await eng.stop()
    assert eng._topic_tagger is None  # cleared at stop
    assert "gliner_topic:media/film" in rec.tags
    assert not any(t.startswith("gliner_topic:") for t in after.tags)


async def test_compile_and_requalify_backfill_with_precedence() -> None:
    eng = await _engine(bound=True)
    try:
        attached = eng._topic_tagger
        eng._topic_tagger = None  # write while the tagger is off: no write-time tags
        ids = {
            "topic": await _fact(eng, "Ilse", "knows", "Bram", "Ilse and Bram watched a film"),
            "caller": await _fact(
                eng, "Tomasz", "knows", "Bram", "Tomasz and Bram saw a film", "domain:work/team"
            ),
            "predicate": await _fact(eng, "Ilse", "resides_in", "Utrecht", "Ilse saw a film"),
            "low": await _fact(eng, "Ilse", "knows", "Ana", "Ilse plays chess with Ana"),
        }
        await compile_graph_edges(eng._pipeline_ctx())
        before = await _domains(eng)
        eng._topic_tagger = attached
        extractor = _stub(eng)
        report = await requalify_namespace(eng._pipeline_ctx(), NS)
        after = await _domains(eng)
        again = await compile_graph_edges(eng._pipeline_ctx())
    finally:
        await eng.stop()
    assert before[ids["topic"]] == {(None, None)}
    assert report["updated"] and report["to_update"]
    assert after[ids["topic"]] == {("media/film", "gliner_topic")}
    assert after[ids["caller"]] == {("work/team", "ingest_tag")}  # caller tag beats topic
    assert after[ids["predicate"]] == {("location/residence", "predicate")}
    assert after[ids["low"]] == {(None, None)}  # below threshold: unknown
    # the tagger only ran where no higher source applied, once per text (cached)
    assert sorted(extractor.texts) == ["Ilse and Bram watched a film", "Ilse plays chess with Ana"]
    assert "written" not in again  # the sleep-cycle compile agrees with the requalify


async def test_bound_tagger_threshold_is_configurable() -> None:
    eng = await _engine(bound=True, min_confidence=0.95)
    try:
        _stub(eng)
        rec = await eng.write("We watched a film", namespace=NS)
    finally:
        await eng.stop()
    assert not any(t.startswith("gliner_topic:") for t in rec.tags)
