"""Behavioral acceptance for selected graph, watch and shared-read paths.

These cases intentionally exercise real SQLite, hash embeddings, LanceDB,
Tantivy and SQLite adjacency. They prove invariants for this selected local
configuration; they are not ranking-quality evaluations or a provider matrix.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from importlib.util import find_spec
from pathlib import Path

import pytest

from memspine import Engine
from memspine.exceptions import ConflictError
from memspine.memories.associative.entities import entity_node_id

NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def _engine(path: Path, *, graph_provider: str = "sql_adjacency") -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        runtime={"deployment_id": f"graph-acceptance-{path.stem}"},
        storage={
            "provider": "sqlite",
            "settings": {"path": str(path), "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {"dim": 64}},
        vector={"provider": "lancedb", "settings": {"isolation": "shared"}},
        cache={"provider": "memory", "settings": {}},
        graph={"provider": graph_provider, "settings": {}},
        read={"hybrid": True, "lexical_provider": "tantivy", "lexical_settings": {}},
        memories={
            "semantic": {"enabled": True},
            "associative": {"enabled": True},
            "prospective": {"enabled": True},
            "shared": {"enabled": True},
        },
    )


@pytest.mark.asyncio
async def test_graph_endpoint_identity_direction_and_record_time_filters(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "graph.sqlite")
    await engine.start()
    try:
        alpha = await engine.write(
            "alpha has a cobalt observability key", namespace="team-a", valid_from=NOW
        )
        beta = await engine.write(
            "beta has a cobalt observability key",
            namespace="team-a",
            valid_from=NOW + timedelta(days=2),
        )
        foreign = await engine.write(
            "foreign cobalt observability key", namespace="team-b", valid_from=NOW
        )
        await engine.associate(alpha.record_id, beta.record_id, namespace="team-a")

        # Link edges keep the event orientation, but associative recall is
        # intentionally undirected. Traversal remains inside the caller scope.
        assert [r.record_id for r in await engine.related(alpha.record_id, "team-a")] == [
            beta.record_id
        ]
        with pytest.raises(ConflictError):
            await engine.related(beta.record_id, "team-b")
        assert foreign.record_id not in {
            r.record_id for r in await engine.related(alpha.record_id, "team-a")
        }

        # Temporal filtering is a record/retrieval contract, not a GraphStore
        # edge-time contract. This validates the selected hybrid record path.
        hits = await engine.search(
            "cobalt observability key",
            namespace="team-a",
            valid_from_before=NOW + timedelta(days=1),
        )
        ids = {record.record_id for record, _ in hits}
        assert alpha.record_id in ids
        assert beta.record_id not in ids

        # Entity-node identity encodes the namespace; equal labels remain
        # distinct projection endpoints. The provider still scopes edge rows.
        graph = engine._graph
        assert graph is not None
        entity_a = entity_node_id("team-a", "acme")
        entity_b = entity_node_id("team-b", "acme")
        await graph.upsert_node(entity_a, namespace="team-a")
        await graph.upsert_node(entity_b, namespace="team-b")
        await graph.upsert_edge(alpha.record_id, entity_a, "mentions", namespace="team-a")
        await graph.upsert_edge(foreign.record_id, entity_b, "mentions", namespace="team-b")
        assert [e.dst for e in await graph.edge_list("team-a") if e.src == alpha.record_id] == [
            entity_a
        ]
        assert entity_a != entity_b
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_prospective_due_reads_are_repeatable_and_namespace_scoped(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "watches.sqlite")
    await engine.start()
    try:
        due_at = NOW + timedelta(hours=1)
        watch_a = await engine.watch("rotate team-a key", namespace="team-a", due_at=due_at)
        watch_b = await engine.watch("rotate team-b key", namespace="team-b", due_at=due_at)

        assert await engine.due("team-a", now=due_at - timedelta(seconds=1)) == []
        first, second = await asyncio.gather(
            engine.due("team-a", now=due_at),
            engine.due("team-a", now=due_at),
        )
        assert [record.record_id for record in first] == [watch_a.record_id]
        assert [record.record_id for record in second] == [watch_a.record_id]
        assert [record.record_id for record in await engine.due("team-b", now=due_at)] == [
            watch_b.record_id
        ]

        await engine.acknowledge_watch(watch_a.record_id, namespace="team-a")
        # Duplicate acknowledgements are idempotent and stop subsequent pulls.
        await engine.acknowledge_watch(watch_a.record_id, namespace="team-a")
        assert await engine.due("team-a", now=due_at + timedelta(seconds=1)) == []
        with pytest.raises(ConflictError):
            await engine.acknowledge_watch(watch_b.record_id, namespace="team-a")
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_shared_search_honors_grant_then_revocation_across_hybrid_reads(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "shared.sqlite")
    await engine.start()
    try:
        content = "violet calibration protocol exact phrase 7319"
        record = await engine.write(content, namespace="owner")
        neighbor = await engine.write(
            "owner-only neighbor record for graph traversal", namespace="owner"
        )
        await engine.associate(record.record_id, neighbor.record_id, namespace="owner")

        # Warm the owner's hybrid lexical/vector projections and graph path.
        owner_hits = await engine.search("violet calibration protocol 7319", namespace="owner")
        assert record.record_id in {candidate.record_id for candidate, _ in owner_hits}
        assert neighbor.record_id in {
            candidate.record_id for candidate in await engine.related(record.record_id, "owner")
        }
        await engine.grant("reader", namespace="owner", memory_types=["semantic"])

        before = await engine.shared_search("violet calibration protocol 7319", namespace="reader")
        assert record.record_id in {candidate.record_id for candidate, _ in before}
        # A shared semantic grant does not widen the graph's namespace scope.
        with pytest.raises(ConflictError):
            await engine.related(record.record_id, "reader")

        await engine.revoke("reader", namespace="owner")
        after = await engine.shared_search("violet calibration protocol 7319", namespace="reader")
        assert record.record_id not in {candidate.record_id for candidate, _ in after}
        # Revocation is evaluated from authoritative grant records on each
        # search; populated lexical/vector projections do not restore access.
        assert (
            engine._vector is not None and engine._lexical is not None and engine._graph is not None
        )
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_literal_query_text_cannot_change_lexical_scope_or_query_grammar(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path / "literal-queries.sqlite")
    await engine.start()
    try:
        local = await engine.write(
            'literal OR wildcard namespace field "quoted"', namespace="tenant/literal"
        )
        foreign = await engine.write(
            'literal OR wildcard namespace field "quoted"', namespace="tenant/foreign"
        )
        lexical = engine._lexical
        assert lexical is not None

        # Quotes, boolean-looking words, field syntax, wildcards, unmatched
        # delimiters and punctuation-only text are data. They must not reach a
        # backend query parser or widen the caller's namespace.
        queries = (
            '"literal" OR *',
            "literal OR namespace:tenant/foreign",
            'field) OR namespace:"tenant/foreign"',
            '"unterminated literal',
            "{}:*()",
        )
        for query in queries:
            lexical_hits = await lexical.search("tenant/literal", query, top_k=10)
            lexical_ids = {hit.record_id for hit in lexical_hits}
            if query == "{}:*()":
                assert not lexical_ids
            else:
                assert local.record_id in lexical_ids
            assert foreign.record_id not in lexical_ids

            public_hits = await engine.search(query, namespace="tenant/literal", top_k=10)
            assert foreign.record_id not in {record.record_id for record, _ in public_hits}
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_accented_latin_exact_terms_retrieve_only_within_namespace(tmp_path: Path) -> None:
    engine = _engine(tmp_path / "accented-latin.sqlite")
    await engine.start()
    try:
        content = "El niño pidió café en Málaga durante el verano."
        local = await engine.write(content, namespace="tenant/spanish")
        foreign = await engine.write(content, namespace="tenant/other-spanish")
        lexical = engine._lexical
        assert lexical is not None

        query = "niño pidió café Málaga"
        lexical_hits = await lexical.search("tenant/spanish", query, top_k=10)
        assert local.record_id in {hit.record_id for hit in lexical_hits}
        assert foreign.record_id not in {hit.record_id for hit in lexical_hits}

        public_hits = await engine.search(query, namespace="tenant/spanish", top_k=10)
        public_ids = {record.record_id for record, _ in public_hits}
        assert local.record_id in public_ids
        assert foreign.record_id not in public_ids
    finally:
        await engine.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "graph_provider",
    ["ladybug"],
)
async def test_ladybug_graph_scope_contract_when_optional_runtime_is_installed(
    tmp_path: Path, graph_provider: str
) -> None:
    if find_spec("ladybug") is None:
        pytest.skip("LadybugDB runtime is not installed in this environment")
    engine = _engine(tmp_path / "ladybug.sqlite", graph_provider=graph_provider)
    await engine.start()
    try:
        graph = engine._graph
        assert graph is not None
        await graph.upsert_edge("left", "right", "related", namespace="scope-a")
        await graph.upsert_edge("left", "elsewhere", "related", namespace="scope-b")
        assert [n.node_id for n in await graph.neighbors("left", namespace="scope-a")] == ["right"]
    finally:
        await engine.stop()
