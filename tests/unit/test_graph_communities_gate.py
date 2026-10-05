"""GP-9 (#23): community summaries are read only when a seed entity is a member.

Two communities (built with the built-in LPA, so no extra is needed): three
linked facts about Melanie, and three linked notes about the weather that name no
entity. With ``read.graph_communities`` on, the weather community's summary never
reaches a search; Melanie's is admitted for a query naming her.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest

from memspine import Engine
from memspine.core.records import MemoryRecord, RecordStatus
from memspine.workers.pipelines import reorganize

MELANIE = [
    ("Melanie read Dune", "Dune"),
    ("Melanie painted a sunrise", "sunrise"),
    ("Melanie adopted a cat", "cat"),
]
WEATHER = [
    "heavy rain flooded the street",
    "a storm knocked the power out",
    "thunder kept everyone awake",
]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {
                "enabled": True,
                "policies": {"entity_nodes": True, "community": {"algorithm": "lpa"}},
            },
        },
        read={"record_access": False, **read},
    )


async def _clique(eng: Engine, ids: list[str]) -> None:
    for i, src in enumerate(ids):
        for dst in ids[i + 1 :]:
            await eng.associate(src, dst, namespace="a")


async def _build(eng: Engine) -> dict[str, MemoryRecord]:
    melanie = []
    for text, dst in MELANIE:
        rec = await eng.write(
            text,
            namespace="a",
            entity="Melanie",
            tags=["kind:event", "rel:did", f"dst:{dst}"],
        )
        melanie.append(rec.record_id)
    weather = [(await eng.write(t, namespace="a")).record_id for t in WEATHER]
    await _clique(eng, melanie)
    await _clique(eng, weather)
    stats = await reorganize(eng._pipeline_ctx())
    assert stats["parents"] == 2, stats
    parents = [
        r
        for r in await eng._require_started().list_records("a", "semantic")
        if r.source.channel == "reorganize" and r.status is RecordStatus.ACTIVATED
    ]
    by_side = {}
    for parent in parents:
        side = "melanie" if set(parent.source.parents) == set(melanie) else "weather"
        by_side[side] = parent
    assert set(by_side) == {"melanie", "weather"}
    return by_side


@pytest.fixture
async def gated() -> AsyncIterator[Engine]:
    eng = _engine(graph_communities=True)
    await eng.start()
    yield eng
    await eng.stop()


async def test_gate_admits_only_communities_of_seed_entities(gated: Engine) -> None:
    parents = await _build(gated)
    admitted = await gated._community_gate("a", "what does Melanie do", [], [], [], False)
    assert admitted == [parents["melanie"].record_id]
    # A seedless query with no fallback hits admits nothing.
    assert await gated._community_gate("a", "the weather", [], [], [], False) == []


async def test_a_summary_without_a_seed_member_is_never_read(gated: Engine) -> None:
    parents = await _build(gated)
    hits = await gated.search("rain storm thunder", namespace="a", top_k=20)
    assert parents["weather"].record_id not in {r.record_id for r, _ in hits}


async def test_without_the_gate_community_summaries_are_ordinary_hits() -> None:
    eng = _engine()
    await eng.start()
    try:
        parents = await _build(eng)
        hits = await eng.search("rain storm thunder", namespace="a", top_k=20)
        assert parents["weather"].record_id in {r.record_id for r, _ in hits}
    finally:
        await eng.stop()


async def test_with_the_graph_leg_admitted_summaries_join_it() -> None:
    eng = _engine(graph_communities=True, graph_leg=True)
    await eng.start()
    try:
        parents = await _build(eng)
        hits = await eng.search("what does Melanie do", namespace="a", top_k=20)
        ids = {r.record_id for r, _ in hits}
        assert parents["melanie"].record_id in ids
        assert parents["weather"].record_id not in ids
    finally:
        await eng.stop()


async def test_membership_goes_through_mentioned_records(gated: Engine) -> None:
    parents = await _build(gated)
    assert gated._associative is not None
    found = await gated._associative.entity_communities("a", ["ent:a:melanie"])
    assert found == [parents["melanie"].record_id]
    # Entities of another namespace are never entered.
    assert await gated._associative.entity_communities("b", ["ent:a:melanie"]) == []
