"""RM-107 (ADR-094): every built-in family declares one generic domain, so ``graph_filter``
by domain matches edges compiled from built-in-pack facts. Model-free."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.derive.pipeline.stages.edge_compiler import (
    COMPILED,
    EdgeCompiler,
    compile_graph_edges,
    edge_qualifiers_for,
)
from memspine.observability import tap
from memspine.read.pipeline import legs as pipeline_legs
from memspine.semantics import FAMILY_DOMAINS, load_builtin_registry, load_registry
from memspine.services.graph.qualifiers import QualifierFilter, edge_qualifiers
from memspine.store.records import MemoryRecord, SourceInfo

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
NS = "a"

#: One built-in fact per generic domain: (subject, predicate, object) -> its family domain.
CORE_FACTS: dict[str, tuple[str, str, str]] = {
    "personal": ("Ilse", "resides_in", "Utrecht"),
    "work": ("Ilse", "employed_by", "Acme"),
    "social": ("Ilse", "friend_of", "Bram"),
    "travel": ("Ilse", "visited", "Porto"),
    "learning": ("Thesis", "cites", "Handbook"),
    "tools": ("Ilse", "instructed", "Assistant"),
    "other": ("Bike", "part_of", "Fleet"),
    # RM-62/RM-63 families activated after their fixtures passed
    "health": ("Ilse", "allergic_to", "Peanuts"),
    "finance": ("Ilse", "purchased", "Bike"),
}


@pytest.fixture(autouse=True)
def _namespace_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pipeline_legs, "_frontier_capable", lambda graph: False)


def _record(src: str, rel: str, dst: str, *tags: str) -> MemoryRecord:
    return MemoryRecord(
        namespace=NS,
        memory_type="semantic",
        content=f"{src} {rel} {dst}",
        entity=src,
        tags=["kind:state", f"rel:{rel}", f"dst:{dst}", *tags],
    )


def _ids(record: MemoryRecord) -> dict[str, dict[str, str]]:
    names = (record.entity or "", next(t[4:] for t in record.tags if t.startswith("dst:")))
    return {record.record_id: {n.lower(): f"ent:{NS}:{n.lower()}" for n in names}}


def _matches(compiler: EdgeCompiler, record: MemoryRecord, flt: QualifierFilter) -> list[bool]:
    compiled = compiler.compile([record], NS, entity_ids=_ids(record))
    meta = compiled.meta[record.record_id]
    return [flt.matches(edge_qualifiers_for(e, meta)) for e in compiled.delta.edges]


def test_every_generic_domain_is_declared_by_a_builtin_family() -> None:
    full = load_registry(["memspine.core", "memspine.proposed"])
    assert full.family_domains == set(FAMILY_DOMAINS)
    assert load_builtin_registry().family_domains == set(FAMILY_DOMAINS)
    assert set(CORE_FACTS) == set(FAMILY_DOMAINS)


@pytest.mark.parametrize("domain", sorted(CORE_FACTS))
def test_domain_filter_matches_core_pack_facts(domain: str) -> None:
    compiler = EdgeCompiler(registry=load_builtin_registry())
    record = _record(*CORE_FACTS[domain])
    hits = _matches(compiler, record, QualifierFilter(topical_domains=frozenset({domain})))
    assert hits and all(hits)
    others = frozenset(set(FAMILY_DOMAINS) - {domain})
    assert not any(_matches(compiler, record, QualifierFilter(topical_domains=others)))


@pytest.mark.parametrize(
    ("domain", "fact"),
    [
        ("health", ("Ilse", "allergic_to", "Peanuts")),
        ("finance", ("Ilse", "purchased", "Bike")),
        ("personal", ("Ilse", "has_routine", "Running")),
        ("tools", ("Agent", "attempted", "Deploy")),
    ],
)
def test_domain_filter_matches_proposed_pack_facts(domain: str, fact: tuple[str, ...]) -> None:
    """The proposed pack's families declare domains too (selected explicitly here)."""
    compiler = EdgeCompiler(registry=load_registry(["memspine.core", "memspine.proposed"]))
    hits = _matches(compiler, _record(*fact), QualifierFilter(topical_domains=frozenset({domain})))
    assert hits and all(hits)


def test_attribute_key_family_beats_the_predicate_family() -> None:
    compiler = EdgeCompiler(registry=load_builtin_registry())
    record = _record("Bike", "has_measurement", "450").model_copy(update={"attribute": "price"})
    got = compiler.domain_of(record)
    assert got is not None and (got.domain, got.source) == ("finance", "family")


# --------------------------------------------------------------------------- engine


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


async def test_search_graph_filter_by_domain_on_builtin_facts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = Engine(
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
        graph={"edge_compiler": True},
        read={"hybrid": True, "record_access": False, "graph_leg": True, "graph_leg_seed_hits": 0},
    )
    await eng.start()
    spy = _Spy(monkeypatch)
    ids: dict[str, str] = {}
    try:
        for domain, (src, rel, dst) in CORE_FACTS.items():
            turn = await eng.write(f"{src} {rel} {dst}.", namespace=NS, memory_type="episodic")
            rec = await eng.write(
                f"{src} {rel} {dst}",
                namespace=NS,
                memory_type="semantic",
                entity=src,
                tags=["kind:state", f"rel:{rel}", f"dst:{dst}"],
                source=SourceInfo(
                    role="derived", channel="extract_graph", parents=[turn.record_id]
                ),
                valid_from=T0,
            )
            ids[domain] = rec.record_id
        await compile_graph_edges(eng._pipeline_ctx())
        assert eng._graph is not None
        edges = [
            e
            for e in await eng._graph.edge_list(NS)
            if e.properties.get("compiled") == COMPILED and e.weight > 0
        ]
        seen: dict[str, dict[str, Any]] = {}
        for domain in CORE_FACTS:
            flt = QualifierFilter(
                known_as_of=datetime.now(UTC) + timedelta(days=1),
                topical_domains=frozenset({domain}),
            )
            await eng.search("Ilse", namespace=NS, top_k=4, graph_filter=flt)
            seen[domain] = spy.last()
    finally:
        await eng.stop()
    for domain, record_id in ids.items():
        mine = [e for e in edges if e.properties["record_id"] == record_id]
        assert mine, domain
        for edge in mine:
            q = edge_qualifiers(edge.properties)
            assert q is not None and q.topical_domain is not None
            assert q.topical_domain.split("/")[0] == domain
        assert seen[domain]["admitted"] == len(mine), domain
