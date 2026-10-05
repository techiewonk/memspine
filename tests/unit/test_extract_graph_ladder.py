"""C2 through the semantic door: background extract_graph facts climb the M4 ladder.

GP-1 promised that a ``state`` edge supersedes the older value while ``event``
edges are add-only. The write-time pipeline (C3) always did that; the background
``extract_graph`` stage now writes through the same door, so the promise holds for
it too. GR-9: a verbatim duplicate edge from another episode adds that episode to
the fact's provenance instead of a new fact.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.prompts.models import ExtractedEdge
from memspine.workers.pipelines import extract_graph

T0 = datetime(2023, 5, 1, tzinfo=UTC)


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
    )
    await eng.start()
    yield eng
    await eng.stop()


def _edges(table: dict[str, list[ExtractedEdge]]):  # type: ignore[no-untyped-def]
    async def fake_extract(content: str, _context: object = None) -> list[ExtractedEdge]:
        return list(table.get(content, []))

    return fake_extract


def _edge(src: str, rel: str, dst: str, kind: str, fact: str) -> ExtractedEdge:
    return ExtractedEdge(
        src_entity=src, rel=rel, dst_entity=dst, kind=kind, fact=fact, confidence=0.9
    )


async def _facts(eng: Engine, ns: str = "a") -> list[MemoryRecord]:
    storage = eng._require_started()
    return [
        r for r in await storage.list_records(ns, "semantic") if r.source.channel == "extract_graph"
    ]


async def _turn(eng: Engine, text: str, day: int) -> MemoryRecord:
    return await eng.write(
        text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(days=day)
    )


async def test_two_state_edges_supersede_through_the_ladder(engine: Engine) -> None:
    engine._extract_edges = _edges(
        {
            "Alice moved to Paris": [
                _edge("Alice", "lives_in", "Paris", "state", "Alice lives in Paris")
            ],
            "Alice moved to London": [
                _edge("Alice", "lives_in", "London", "state", "Alice lives in London")
            ],
        }
    )
    await _turn(engine, "Alice moved to Paris", 0)
    await _turn(engine, "Alice moved to London", 30)
    stats = await extract_graph(engine._pipeline_ctx())
    assert stats["edges_written"] == 2
    facts = {f.content: f for f in await _facts(engine)}
    assert facts["Alice lives in London"].status is RecordStatus.ACTIVATED
    assert facts["Alice lives in Paris"].status is RecordStatus.ARCHIVED


async def test_two_event_edges_both_stay_active(engine: Engine) -> None:
    engine._extract_edges = _edges(
        {
            "Alice finished Dune": [_edge("Alice", "read", "Dune", "event", "Alice read Dune")],
            "Alice finished Emma": [_edge("Alice", "read", "Emma", "event", "Alice read Emma")],
        }
    )
    await _turn(engine, "Alice finished Dune", 0)
    await _turn(engine, "Alice finished Emma", 3)
    await extract_graph(engine._pipeline_ctx())
    facts = await _facts(engine)
    assert sorted(f.content for f in facts) == ["Alice read Dune", "Alice read Emma"]
    assert all(f.status is RecordStatus.ACTIVATED and f.attribute is None for f in facts)


async def test_verbatim_duplicate_edge_adds_its_episode_as_provenance(engine: Engine) -> None:
    calls: list[str] = []
    rome = _edge("Alice", "visited", "Rome", "event", "Alice visited Rome")
    inner = _edges({"Alice was in Rome": [rome], "Rome again for Alice": [rome]})

    async def counting(content: str, context: object = None) -> list[ExtractedEdge]:
        calls.append(content)
        return await inner(content, context)

    engine._extract_edges = counting
    first = await _turn(engine, "Alice was in Rome", 0)
    second = await _turn(engine, "Rome again for Alice", 5)
    stats = await extract_graph(engine._pipeline_ctx())
    assert stats["edges_written"] == 1 and stats["provenance_added"] == 1
    [fact] = await _facts(engine)
    assert fact.source.parents == [first.record_id]
    assert f"{constants.EDGE_SOURCE_TAG_PREFIX}{second.record_id}" in fact.tags
    assert len(calls) == 2  # one extractor call per source; the duplicate costs none

    # A later sweep restating it from a third episode adds one more source.
    third = await _turn(engine, "Alice was in Rome", 9)
    stats = await extract_graph(engine._pipeline_ctx())
    assert stats["provenance_added"] == 1 and stats["edges_written"] == 0
    [fact] = await _facts(engine)
    assert f"{constants.EDGE_SOURCE_TAG_PREFIX}{third.record_id}" in fact.tags


async def test_a_duplicate_with_another_kind_is_not_verbatim(engine: Engine) -> None:
    engine._extract_edges = _edges(
        {
            "one": [_edge("Bo", "likes", "tea", "event", "Bo likes tea")],
            "two": [_edge("Bo", "likes", "tea", "state", "Bo likes tea")],
        }
    )
    await _turn(engine, "one", 0)
    await _turn(engine, "two", 1)
    stats = await extract_graph(engine._pipeline_ctx())
    assert stats["provenance_added"] == 0
    [fact] = await _facts(engine)
    assert not any(t.startswith(constants.EDGE_SOURCE_TAG_PREFIX) for t in fact.tags)
