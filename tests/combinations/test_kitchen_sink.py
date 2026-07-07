"""C6 §6: the "everything on" boot — all nine memory types at once.

One engine with every type enabled, a round-trip that touches several types in
one session, ``describe()`` listing all nine, and a clean ``stop()``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memspine import Engine
from memspine.core.registry import MEMORY_TYPES

ALL_TYPES = sorted(MEMORY_TYPES)

#: A fixed instant so time-triggered verbs (watch/due) never touch the wall clock.
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


@pytest.fixture
async def kitchen(make_engine: Callable[..., Engine]) -> AsyncIterator[Engine]:
    engine = make_engine(memories={name: {"enabled": True} for name in ALL_TYPES})
    await engine.start()
    yield engine
    await engine.stop()


async def test_all_nine_enabled_in_describe(kitchen: Engine) -> None:
    world = kitchen.describe()
    assert set(world["memories"]["enabled"]) == set(ALL_TYPES)
    # Every type-specific flag is on at once.
    for flag in (
        "episodic",
        "semantic_pipeline",
        "procedural",
        "reflective",
        "resource_ingest",
        "associative",
        "prospective",
        "shared",
    ):
        assert world[flag] is True
    assert world["graph"] == "SQLiteAdjacencyGraph"  # associative wired the graph
    assert "graph" in world["projectors"]


async def test_kitchen_sink_multi_type_round_trip(kitchen: Engine, tmp_path: Path) -> None:
    # working + semantic + episodic
    work = await kitchen.write("scratch note", memory_type="working")
    fact = await kitchen.write("the deploy pipeline uses blue-green rollout")
    episode = await kitchen.write("we deployed on friday", memory_type="episodic")
    assert work.record_id in {r.record_id for r in await kitchen.retrieve(memory_type="working")}
    assert fact.record_id in {r.record_id for r, _ in await kitchen.search("deploy pipeline")}

    # procedural
    skill = await kitchen.add_skill("1. build 2. ship", name="release")
    assert skill.record_id in {r.record_id for r in await kitchen.skills(usable_only=False)}

    # reflective (over the real episode)
    refl = await kitchen.reflect("the team ships on fridays", [episode.record_id])
    assert refl.record_id in {r.record_id for r in await kitchen.retrieve(memory_type="reflective")}

    # associative (explicit disjoint link)
    left = await kitchen.write("alpha only")
    right = await kitchen.write("omega only")
    await kitchen.associate(left.record_id, right.record_id, weight=0.9)
    assert [r.record_id for r in await kitchen.related(left.record_id)] == [right.record_id]

    # prospective
    due_at = NOW + timedelta(hours=1)
    watch = await kitchen.watch("rotate the api key", due_at=due_at)
    assert [r.record_id for r in await kitchen.due(now=due_at + timedelta(seconds=1))] == [
        watch.record_id
    ]

    # resource
    doc = tmp_path / "runbook.md"
    doc.write_text("# Runbook\n\nStep one.\n\nStep two.", encoding="utf-8")
    chunks = await kitchen.ingest(str(doc))
    assert chunks

    # shared (cross-namespace grant)
    shared_fact = await kitchen.write("shared rotation policy", namespace="team-a")
    await kitchen.grant("team-b", namespace="team-a")
    seen = await kitchen.shared_search("rotation policy", namespace="team-b")
    assert shared_fact.record_id in {r.record_id for r, _ in seen}
