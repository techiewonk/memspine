"""RM-41 provider contract: qualifier-filtered traversal answers identically per adapter.

Every graph provider that declares ``supports_qualifier_filters`` must pass these
fixtures; a provider that does not must raise :class:`UnsupportedQualifierFilterError`
instead of silently ignoring a filter. To run the contract against a new provider, add
it to :data:`ADAPTERS` and the ``store`` fixture below.

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

from collections.abc import AsyncIterator, Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.services.graph.base import GraphStore
from memspine.services.graph.qualifiers import (
    QUALIFIERS_PROP,
    AssertionModality,
    ClaimState,
    Completeness,
    EdgeQualifiers,
    ErasureState,
    LifecycleStatus,
    Polarity,
    Provenance,
    QualifiedGraphStore,
    QualifierFilter,
    SensitivityClass,
    UnsupportedQualifierFilterError,
    edge_qualifiers,
    require_qualifier_support,
    with_qualifiers,
)
from memspine.services.storage.sqlite.schema import metadata

ADAPTERS = ["sqlite_adjacency", "ladybug"]
NS = "ns/q"


def _t(year: int, month: int = 1, day: int = 1) -> datetime:
    return datetime(year, month, day, tzinfo=UTC)


@pytest.fixture(params=ADAPTERS)
async def store(request: pytest.FixtureRequest) -> AsyncIterator[GraphStore]:
    if request.param == "ladybug":
        pytest.importorskip("ladybug")
        from memspine.clients.ladybug import LadybugClient
        from memspine.services.graph.ladybug import LadybugGraphStore

        ladybug_client = LadybugClient(":memory:")
        await ladybug_client.connect()
        yield LadybugGraphStore(ladybug_client)
        await ladybug_client.close()
    else:
        sqlite_client = SQLiteClient(":memory:")
        await sqlite_client.connect()
        async with sqlite_client.engine.begin() as conn:
            await conn.run_sync(metadata.create_all)
        from memspine.services.graph.sqlite_adjacency import SQLiteAdjacencyGraph

        yield SQLiteAdjacencyGraph(sqlite_client)
        await sqlite_client.close()


FULL = EdgeQualifiers(
    valid_from=_t(2026),
    valid_to=_t(2026, 6),
    known_at=_t(2026, 1, 2),
    assertion_modality=AssertionModality.ASSERTED,
    polarity=Polarity.POSITIVE,
    lifecycle_status=LifecycleStatus.IN_PROGRESS,
    claim_state=ClaimState.CURRENT,
    provenance=Provenance(principal="user", source_id="msg-1", origin_type="transcript"),
    trust_tier="verified",
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
            sensitivity=SensitivityClass.LOCATION,
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
    ("sensitivity_location", {"sensitivities": {SensitivityClass.LOCATION}}, 2, ["c"]),
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


async def test_neo4j_declares_unsupported_and_raises() -> None:
    from memspine.services.graph.neo4j import Neo4jGraphStore

    class _NoExec:
        async def run(self, op: str, params: Mapping[str, Any]) -> list[dict[str, Any]]:
            raise AssertionError("a qualifier filter must not reach the executor")

        async def close(self) -> None:
            return None

    neo = Neo4jGraphStore(_NoExec())
    assert neo.supports_qualifier_filters is False
    assert isinstance(neo, QualifiedGraphStore)
    with pytest.raises(UnsupportedQualifierFilterError, match="Neo4jGraphStore"):
        require_qualifier_support(neo)
    with pytest.raises(UnsupportedQualifierFilterError, match="neo4j"):
        await neo.neighbors_qualified("a", QualifierFilter())
    with pytest.raises(UnsupportedQualifierFilterError, match="neo4j"):
        await neo.subgraph_qualified(["a"], QualifierFilter())
