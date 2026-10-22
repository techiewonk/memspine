"""RM-41 provider contract: qualifier-filtered traversal answers identically per adapter.

Every graph provider that declares ``supports_qualifier_filters`` must pass these
fixtures; a provider that does not must raise :class:`UnsupportedQualifierFilterError`
instead of silently ignoring a filter. To run the contract against a new provider, add
it to :data:`ADAPTERS` and the ``store`` fixture below. Every adapter (Neo4j too)
also validates qualifiers on write (RM-84): malformed ones are dropped with a
``graph.qualifiers_dropped`` warning and the edge is stored unqualified.

Neo4j (RM-94) runs twice: ``neo4j_fake`` over the in-memory ``FakeNeo4j`` recorder of the
F-16e tests (always), and ``neo4j`` against a live server when the driver is installed and
``MEMSPINE_TEST_NEO4J_URL`` is set (plus optional ``MEMSPINE_TEST_NEO4J_USER`` /
``MEMSPINE_TEST_NEO4J_PASSWORD``), under a private label that is cleared afterwards.

The fixture graph (namespace ``ns/q``, seed ``a``)::

    a -works_on-> b   full P1/P2/P3 qualifiers, valid [2026-01, 2026-06), domain work
    a -lives_in-> c   valid [2025-01, open-ended), domain residence, sensitivity location
    a -related->  d   no qualifiers at all (every qualifier unknown)
    a -lives_in-> f   valid from 2024-01, end UNKNOWN (not open-ended), domain residence
    b -cites->    e   valid [2026-03, 2026-04), domain work, claim superseded
    c -related->  g   valid [2025-02, open-ended), domain residence
    a -works_on-> x   in namespace ns/other with passing qualifiers (must never be reached)
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import uuid
from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs

from memspine.clients.sqlite import SQLiteClient
from memspine.services.graph.base import GraphStore
from memspine.services.graph.qualifiers import (
    QUALIFIERS_PROP,
    AssertionModality,
    ClaimState,
    Completeness,
    EdgeQualifiers,
    ErasureState,
    Polarity,
    Provenance,
    QualifiedGraphStore,
    QualifierFilter,
    UnsupportedQualifierFilterError,
    edge_qualifiers,
    require_qualifier_support,
    with_qualifiers,
)
from memspine.services.storage.sqlite.schema import metadata

ADAPTERS = ["sql_adjacency", "sql_adjacency_postgres", "ladybug", "neo4j_fake", "neo4j"]
LIVE_NEO4J = bool(os.environ.get("MEMSPINE_TEST_NEO4J_URL")) and (
    importlib.util.find_spec("neo4j") is not None
)
#: ADR-081: ``sql_adjacency`` on Postgres runs when a scratch database is given.
PG_URL = os.environ.get("MEMSPINE_TEST_PG_URL")
NS = "ns/q"


def _t(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


@pytest.fixture(params=ADAPTERS)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[GraphStore]:
    if request.param in ("neo4j", "neo4j_fake"):
        from memspine.services.graph.neo4j import Neo4jGraphStore, executor_from_env

        if request.param == "neo4j_fake":
            sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "wiring"))
            from test_f_16e_neo4j_outbox import FakeNeo4j

            yield Neo4jGraphStore(FakeNeo4j(), scoped=True)
            return
        if not LIVE_NEO4J:
            pytest.skip("needs the neo4j driver and MEMSPINE_TEST_NEO4J_URL (a live server)")
        ex = executor_from_env(
            url_env="MEMSPINE_TEST_NEO4J_URL",
            user_env="MEMSPINE_TEST_NEO4J_USER",
            password_env="MEMSPINE_TEST_NEO4J_PASSWORD",
            node_label=f"MemQual{uuid.uuid4().hex[:10]}",
            rel_type=f"MemQualRel{uuid.uuid4().hex[:10]}",
        )
        try:
            yield Neo4jGraphStore(ex, scoped=True)  # scoped, as the provider builds it
        finally:
            await ex.run("clear", {})
            await ex.close()
        return
    if request.param == "ladybug":
        pytest.importorskip("ladybug")
        from memspine.clients.ladybug import LadybugClient
        from memspine.services.graph.ladybug import LadybugGraphStore

        ladybug_client = LadybugClient(":memory:")
        await ladybug_client.connect()
        yield LadybugGraphStore(ladybug_client)
        await ladybug_client.close()
    elif request.param == "sql_adjacency_postgres":
        if not PG_URL or importlib.util.find_spec("psycopg") is None:
            pytest.skip("sql_adjacency on Postgres: set MEMSPINE_TEST_PG_URL (scratch database)")
        from sqlalchemy import delete

        from memspine.clients.postgres import PostgresClient
        from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph
        from memspine.services.storage.sqlite.schema import graph_edges, graph_nodes

        pg_client = PostgresClient(PG_URL)
        await pg_client.connect()
        async with pg_client.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
            await conn.execute(delete(graph_edges))
            await conn.execute(delete(graph_nodes))
        try:
            yield SqlAdjacencyGraph(pg_client)
        finally:
            async with pg_client.engine.begin() as conn:
                await conn.execute(delete(graph_edges))
                await conn.execute(delete(graph_nodes))
            await pg_client.close()
    else:
        sqlite_client = SQLiteClient(":memory:")
        await sqlite_client.connect()
        async with sqlite_client.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        from memspine.services.graph.sql_adjacency import SqlAdjacencyGraph

        yield SqlAdjacencyGraph(sqlite_client)
        await sqlite_client.close()


FULL = EdgeQualifiers(
    valid_from=_t(2026),
    valid_to=_t(2026, 6),
    known_at=_t(2026, 1, 2),
    assertion_modality=AssertionModality.ASSERTED,
    polarity=Polarity.POSITIVE,
    lifecycle_status="in_progress",
    claim_state=ClaimState.CURRENT,
    provenance=Provenance(principal="user", source_id="msg-1", origin_type="transcript"),
    trust=0.9,
    subject_binding="person:alice",
    scope="private",
    grants=("team",),
    erasure_state=ErasureState.ACTIVE,
    event_role="actor",
    vocabulary_pack="core@1",
    topical_domain="work",
    extraction_confidence=0.9,
    extraction_method="rule",
    completeness=Completeness.COMPLETE,
    independent_support_count=2,
)

FIXTURE: list[tuple[str, str, str, EdgeQualifiers | None, str]] = [
    ("a", "b", "works_on", FULL, NS),
    (
        "a",
        "c",
        "lives_in",
        EdgeQualifiers(
            valid_from=_t(2025),
            valid_open_ended=True,
            known_at=_t(2025, 1, 5),
            assertion_modality=AssertionModality.ASSERTED,
            claim_state=ClaimState.CURRENT,
            topical_domain="residence",
            sensitivity=frozenset({"location"}),
            sensitivity_checked=True,
            extraction_confidence=0.6,
            independent_support_count=1,
        ),
        NS,
    ),
    ("a", "d", "related", None, NS),
    (
        "a",
        "f",
        "lives_in",
        EdgeQualifiers(valid_from=_t(2024), known_at=_t(2024, 1, 2), topical_domain="residence"),
        NS,
    ),
    (
        "b",
        "e",
        "cites",
        EdgeQualifiers(
            valid_from=_t(2026, 3),
            valid_to=_t(2026, 4),
            known_at=_t(2026, 3, 2),
            claim_state=ClaimState.SUPERSEDED,
            topical_domain="work",
        ),
        NS,
    ),
    (
        "c",
        "g",
        "related",
        EdgeQualifiers(
            valid_from=_t(2025, 2),
            valid_open_ended=True,
            known_at=_t(2025, 2, 1),
            claim_state=ClaimState.CURRENT,
            topical_domain="residence",
        ),
        NS,
    ),
    ("a", "x", "works_on", FULL, "ns/other"),
]


async def _load(store: GraphStore) -> None:
    for src, dst, rel, quals, ns in FIXTURE:
        # d (unqualified) is the strongest edge, so a cap that ignored the filter shows.
        props: dict[str, object] = {"weight": 0.9 if dst == "d" else 0.5}
        if quals is not None:
            props = with_qualifiers(props, quals)
        await store.upsert_edge(src, dst, rel, props, namespace=ns)


ALL_CLAIM_STATES = frozenset(ClaimState)

CASES: list[tuple[str, dict[str, Any], int, list[str]]] = [
    ("empty_filter_passes_everything", {}, 2, ["b", "c", "d", "f", "e", "g"]),
    ("domain_work", {"topical_domains": {"work"}}, 2, ["b", "e"]),
    ("domain_residence_depth1", {"topical_domains": {"residence"}}, 1, ["c", "f"]),
    ("valid_at_point", {"valid_at": _t(2026, 2)}, 2, ["b", "c", "g"]),
    (
        "valid_window_overlap",
        {"valid_window": (_t(2026, 3, 15), _t(2026, 3, 20))},
        2,
        ["b", "c", "e", "g"],
    ),
    ("valid_window_before_everything", {"valid_window": (_t(2020), _t(2021))}, 2, []),
    ("known_as_of", {"known_as_of": _t(2025, 12, 31)}, 2, ["c", "f", "g"]),
    (
        "unknown_claim_state_never_passes",
        {"claim_states": ALL_CLAIM_STATES},
        2,
        ["b", "c", "e", "g"],
    ),
    ("origin_type", {"origin_types": {"transcript"}}, 2, ["b"]),
    ("required_grant", {"required_grants": {"team"}}, 2, ["b"]),
    ("min_support", {"min_independent_support": 2}, 2, ["b"]),
    ("sensitivity_location", {"sensitivities": {"location"}}, 2, ["c"]),
    (
        "combined_domain_state_confidence",
        {
            "topical_domains": {"work"},
            "claim_states": {ClaimState.CURRENT},
            "min_extraction_confidence": 0.8,
        },
        2,
        ["b"],
    ),
    (
        "combined_temporal_and_domain",
        {"topical_domains": {"residence"}, "valid_at": _t(2026, 2)},
        2,
        ["c", "g"],
    ),
]


@pytest.mark.parametrize(("name", "spec", "depth", "expected"), CASES, ids=[c[0] for c in CASES])
async def test_qualified_neighbors_contract(
    store: GraphStore, name: str, spec: Mapping[str, Any], depth: int, expected: list[str]
) -> None:
    await _load(store)
    qstore = require_qualifier_support(store)
    found = await qstore.neighbors_qualified(
        "a", QualifierFilter.model_validate(dict(spec)), depth=depth, namespace=NS
    )
    assert [n.node_id for n in found] == expected, name


async def test_namespace_isolation_under_filter(store: GraphStore) -> None:
    await _load(store)
    qstore = require_qualifier_support(store)
    flt = QualifierFilter(topical_domains=frozenset({"work"}))
    assert [n.node_id for n in await qstore.neighbors_qualified("a", flt, namespace=NS)] == ["b"]
    other = await qstore.neighbors_qualified("a", flt, namespace="ns/other")
    assert [n.node_id for n in other] == ["x"]


async def test_rel_type_and_cap_apply_to_admitted_edges(store: GraphStore) -> None:
    await _load(store)
    qstore = require_qualifier_support(store)
    residence = QualifierFilter(topical_domains=frozenset({"residence"}))
    by_rel = await qstore.neighbors_qualified("a", residence, rel_type="lives_in", namespace=NS)
    assert [n.node_id for n in by_rel] == ["c", "f"]
    # The cap ranks only admitted neighbours: d (no qualifiers) never takes a slot.
    flt = QualifierFilter(claim_states=ALL_CLAIM_STATES)
    unfiltered = await store.neighbors("a", depth=1, namespace=NS, max_degree=1)
    assert [n.node_id for n in unfiltered] == ["d"]
    capped = await qstore.neighbors_qualified("a", flt, depth=1, namespace=NS, max_degree=1)
    assert [n.node_id for n in capped] == ["b"]


async def test_tombstones_stay_gone_under_filter(store: GraphStore) -> None:
    await _load(store)
    qstore = require_qualifier_support(store)
    await store.upsert_edge(
        "a", "b", "works_on", with_qualifiers({"weight": 0.0}, FULL), namespace=NS
    )
    flt = QualifierFilter(topical_domains=frozenset({"work"}))
    assert await qstore.neighbors_qualified("a", flt, depth=2, namespace=NS) == []


async def test_qualified_subgraph(store: GraphStore) -> None:
    await _load(store)
    qstore = require_qualifier_support(store)
    flt = QualifierFilter(topical_domains=frozenset({"work"}))
    sub = await qstore.subgraph_qualified(["a"], flt, depth=2, namespace=NS)
    assert [(e.src, e.dst, e.rel_type) for e in sub] == [
        ("a", "b", "works_on"),
        ("b", "e", "cites"),
    ]
    assert all(edge_qualifiers(e.properties) is not None for e in sub)
    assert await qstore.subgraph_qualified([], flt, depth=2, namespace=NS) == []


async def test_qualifiers_round_trip_through_the_store(store: GraphStore) -> None:
    await _load(store)
    edges = {(e.src, e.dst): e for e in await store.edge_list(NS)}
    assert edge_qualifiers(edges[("a", "b")].properties) == FULL
    assert edge_qualifiers(edges[("a", "d")].properties) is None
    assert QUALIFIERS_PROP not in edges[("a", "d")].properties
    # The unfiltered walk is unchanged by qualifiers.
    assert {n.node_id for n in await store.neighbors("a", namespace=NS)} == {"b", "c", "d", "f"}


def _keys(edges: list[Any]) -> list[tuple[str, str, str]]:
    return sorted((e.src, e.dst, e.rel_type) for e in edges)


async def test_frontier_edges_contract(store: GraphStore) -> None:
    """RM-104: the public ``frontier_edges`` (live edges touching the nodes) per adapter."""
    await _load(store)
    qstore = require_qualifier_support(store)
    assert isinstance(qstore, QualifiedGraphStore)
    around_a = await qstore.frontier_edges(["a"], namespace=NS)
    assert _keys(around_a) == [
        ("a", "b", "works_on"),
        ("a", "c", "lives_in"),
        ("a", "d", "related"),
        ("a", "f", "lives_in"),
    ]  # ns/other's a->x never leaks in; qualifiers are not filtered
    by_ab = {(e.src, e.dst): e for e in around_a}
    assert edge_qualifiers(by_ab[("a", "b")].properties) == FULL  # properties intact
    # either direction; an edge with both endpoints in the set comes back once
    assert _keys(await qstore.frontier_edges(["b", "a", "b"], namespace=NS)) == [
        ("a", "b", "works_on"),
        ("a", "c", "lives_in"),
        ("a", "d", "related"),
        ("a", "f", "lives_in"),
        ("b", "e", "cites"),
    ]
    assert _keys(await qstore.frontier_edges(["e"], namespace=NS)) == [("b", "e", "cites")]
    lives = await qstore.frontier_edges(["a"], namespace=NS, rel_type="lives_in")
    assert _keys(lives) == [("a", "c", "lives_in"), ("a", "f", "lives_in")]
    assert _keys(await qstore.frontier_edges(["a"], namespace="ns/other")) == [
        ("a", "x", "works_on")
    ]
    assert ("a", "x", "works_on") in _keys(await qstore.frontier_edges(["a"]))  # None = any ns
    assert await qstore.frontier_edges([], namespace=NS) == []
    assert await qstore.frontier_edges(["nobody"], namespace=NS) == []
    # tombstones (weight <= 0) are not live
    await store.upsert_edge(
        "a", "b", "works_on", with_qualifiers({"weight": 0.0}, FULL), namespace=NS
    )
    assert ("a", "b", "works_on") not in _keys(await qstore.frontier_edges(["a"], namespace=NS))


MALFORMED: list[tuple[str, object, str]] = [
    ("not_a_mapping", "current", "must be a mapping"),
    ("unknown_value", {"claim_state": "invented"}, "claim_state"),
    ("unknown_field", {"no_such_qualifier": 1}, "no_such_qualifier"),
    ("naive_time", {"valid_from": "2026-01-01T00:00:00"}, "timezone-aware"),
    ("tier_mismatch", {"trust": 0.9, "trust_tier": "low"}, "trust_tier does not match"),
]


@pytest.mark.parametrize(("name", "raw", "why"), MALFORMED, ids=[m[0] for m in MALFORMED])
async def test_malformed_qualifiers_are_dropped_with_a_warning(
    store: GraphStore, name: str, raw: object, why: str
) -> None:
    """RM-84: invalid qualifiers never fail the write; the edge is stored unqualified."""
    with capture_logs() as logs:
        await store.upsert_edge(
            "a", "b", "works_on", {"weight": 0.7, QUALIFIERS_PROP: raw, "k": 1}, namespace=NS
        )
    (edge,) = await store.edge_list(NS)
    assert QUALIFIERS_PROP not in edge.properties, name
    assert edge.properties["k"] == 1 and edge.weight == pytest.approx(0.7)  # rest kept
    dropped = [entry for entry in logs if entry["event"] == "graph.qualifiers_dropped"]
    assert len(dropped) == 1 and dropped[0]["log_level"] == "warning"
    entry = dropped[0]
    assert (entry["src"], entry["dst"], entry["rel_type"]) == ("a", "b", "works_on")
    assert why in entry["error"]
    # unqualified now: the empty filter passes it, any constraint does not
    qstore = require_qualifier_support(store)
    assert [
        n.node_id for n in await qstore.neighbors_qualified("a", QualifierFilter(), namespace=NS)
    ] == ["b"]
    flt = QualifierFilter(claim_states=ALL_CLAIM_STATES)
    assert await qstore.neighbors_qualified("a", flt, namespace=NS) == []


async def test_valid_qualifiers_are_stored_without_a_warning(store: GraphStore) -> None:
    with capture_logs() as logs:
        await store.upsert_edge("a", "b", "works_on", with_qualifiers({}, FULL), namespace=NS)
    assert not [e for e in logs if e["event"] == "graph.qualifiers_dropped"]
    (edge,) = await store.edge_list(NS)
    assert edge_qualifiers(edge.properties) == FULL


async def test_neo4j_drops_malformed_qualifiers_with_a_warning() -> None:
    from memspine.services.graph.neo4j import Neo4jGraphStore

    calls: list[tuple[str, Mapping[str, Any]]] = []

    class _Recorder:
        async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            calls.append((op, params))
            return []

        async def close(self) -> None:
            return None

    neo = Neo4jGraphStore(_Recorder())
    with capture_logs() as logs:
        await neo.upsert_edge("a", "b", "r", {QUALIFIERS_PROP: {"polarity": "maybe"}}, namespace=NS)
        await neo.upsert_edge("a", "c", "r", with_qualifiers({}, FULL), namespace=NS)
    stored = [json.loads(p["props"]) for op, p in calls if op == "upsert_edge"]
    assert QUALIFIERS_PROP not in stored[0]
    assert edge_qualifiers(stored[1]) == FULL
    dropped = [e for e in logs if e["event"] == "graph.qualifiers_dropped"]
    assert len(dropped) == 1 and dropped[0]["provider"] == "neo4j"


async def test_neo4j_declares_support_and_uses_the_frontier_op() -> None:
    """RM-94: Neo4j runs the shared walk over the ``frontier_edges`` op only."""
    from memspine.services.graph.neo4j import CYPHER, Neo4jGraphStore

    calls: list[str] = []

    class _Recorder:
        async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            calls.append(op)
            return []

        async def close(self) -> None:
            return None

    neo = Neo4jGraphStore(_Recorder())
    assert neo.supports_qualifier_filters is True
    assert isinstance(neo, QualifiedGraphStore)
    assert require_qualifier_support(neo) is neo
    assert await neo.neighbors_qualified("a", QualifierFilter(), depth=2) == []
    assert await neo.subgraph_qualified(["a"], QualifierFilter()) == []
    assert set(calls) - {"schema"} == {"frontier_edges"}
    assert "r.weight > 0" in CYPHER["frontier_edges"]
    assert "$ns IS NULL OR r.namespace = $ns" in CYPHER["frontier_edges"]


def test_unsupported_store_still_raises() -> None:
    class _Plain:
        supports_qualifier_filters = False

    with pytest.raises(UnsupportedQualifierFilterError, match="_Plain"):
        require_qualifier_support(_Plain())
