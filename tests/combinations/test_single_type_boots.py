"""C6 §6: nine single-type minimal configs.

Each boot enables exactly one memory type (no template — a truly minimal
config), runs ONE write→read round-trip through that type's real verb, and
asserts:

* the type is enabled and its C1(b) auto-enabled dependencies actually got
  enabled (the closure is reflected in ``describe()``), and
* the type-specific ``describe()`` flag is on.

The round-trip logic differs per type, so each type carries its own coroutine
in :data:`ROUND_TRIPS`; the boot/closure assertions are shared.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memspine import Engine

#: A fixed instant so time-triggered verbs (watch/due) never touch the wall clock.
NOW = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)

# type -> the dependencies C1(b) must auto-enable when only that type is asked for.
AUTO_ENABLED_DEPS: dict[str, frozenset[str]] = {
    "working": frozenset(),
    "episodic": frozenset(),
    "semantic": frozenset(),
    "procedural": frozenset(),
    "resource": frozenset(),
    "reflective": frozenset({"episodic"}),
    "associative": frozenset({"semantic"}),
    "prospective": frozenset({"semantic"}),
    "shared": frozenset({"semantic"}),  # any-of {semantic, episodic} -> first
}

# type -> the describe() boolean flag it flips on (working is list-only).
DESCRIBE_FLAG: dict[str, str] = {
    "episodic": "episodic",
    "semantic": "semantic_pipeline",
    "procedural": "procedural",
    "reflective": "reflective",
    "resource": "resource_ingest",
    "associative": "associative",
    "prospective": "prospective",
    "shared": "shared",
}


async def _rt_working(engine: Engine, tmp_path: Path) -> None:
    rec = await engine.write("hot working note alpha", memory_type="working")
    got = await engine.retrieve(memory_type="working")
    assert rec.record_id in {r.record_id for r in got}


async def _rt_episodic(engine: Engine, tmp_path: Path) -> None:
    rec = await engine.write("we shipped the release on friday", memory_type="episodic")
    got = await engine.retrieve(memory_type="episodic")
    assert rec.record_id in {r.record_id for r in got}


async def _rt_semantic(engine: Engine, tmp_path: Path) -> None:
    rec = await engine.write("the deploy pipeline uses blue-green rollout")
    got = await engine.search("deploy pipeline rollout")
    assert rec.record_id in {r.record_id for r, _ in got}


async def _rt_procedural(engine: Engine, tmp_path: Path) -> None:
    rec = await engine.add_skill("1. build 2. test 3. ship", name="release")
    got = await engine.skills(usable_only=False)  # drafts included
    assert rec.record_id in {r.record_id for r in got}


async def _rt_reflective(engine: Engine, tmp_path: Path) -> None:
    parent = await engine.write("user asked for terse answers", memory_type="episodic")
    refl = await engine.reflect("the user prefers brevity", [parent.record_id])
    got = await engine.retrieve(memory_type="reflective")
    assert refl.record_id in {r.record_id for r in got}


async def _rt_resource(engine: Engine, tmp_path: Path) -> None:
    doc = tmp_path / "notes.md"
    doc.write_text(
        "# Runbook\n\nRotate the API key monthly.\n\nPage the on-call.", encoding="utf-8"
    )
    chunks = await engine.ingest(str(doc))
    assert chunks, "ingest produced no resource records"
    got = await engine.retrieve(memory_type="resource")
    assert {c.record_id for c in chunks} <= {r.record_id for r in got}


async def _rt_associative(engine: Engine, tmp_path: Path) -> None:
    # Disjoint token sets: the hash embedder finds no similarity, so the only
    # link is the explicit one (no evolution noise).
    a = await engine.write("alpha one")
    b = await engine.write("bravo two")
    await engine.associate(a.record_id, b.record_id, weight=0.9)
    related = await engine.related(a.record_id)
    assert [r.record_id for r in related] == [b.record_id]


async def _rt_prospective(engine: Engine, tmp_path: Path) -> None:
    due_at = NOW + timedelta(hours=1)
    watch = await engine.watch("rotate the api key", due_at=due_at)
    assert await engine.due(now=due_at - timedelta(seconds=1)) == []
    fired = await engine.due(now=due_at + timedelta(seconds=1))
    assert [r.record_id for r in fired] == [watch.record_id]


async def _rt_shared(engine: Engine, tmp_path: Path) -> None:
    fact = await engine.write("the rotation schedule is monthly", namespace="a")
    await engine.grant("b", namespace="a")
    results = await engine.shared_search("rotation schedule", namespace="b")
    assert fact.record_id in {r.record_id for r, _ in results}


ROUND_TRIPS: dict[str, Callable[[Engine, Path], Awaitable[None]]] = {
    "working": _rt_working,
    "episodic": _rt_episodic,
    "semantic": _rt_semantic,
    "procedural": _rt_procedural,
    "reflective": _rt_reflective,
    "resource": _rt_resource,
    "associative": _rt_associative,
    "prospective": _rt_prospective,
    "shared": _rt_shared,
}


@pytest.mark.parametrize("memory_type", sorted(ROUND_TRIPS))
async def test_single_type_boot_round_trip(memory_type: str, tmp_path: Path, make_engine) -> None:
    engine = make_engine(memories={memory_type: {"enabled": True}})
    await engine.start()
    try:
        world = engine.describe()
        enabled = set(world["memories"]["enabled"])
        auto = set(world["memories"]["auto_enabled"])
        deps = AUTO_ENABLED_DEPS[memory_type]

        # The type itself is enabled, and every closure dependency is BOTH
        # enabled and reported as auto-enabled (D-13 is never a silent surprise).
        assert memory_type in enabled
        assert deps <= enabled
        assert deps <= auto
        # Nothing extra crept in: exactly {type} + its dependency closure.
        assert enabled == {memory_type} | set(deps)

        # The describe() flag mirrors the wiring (working is list-only).
        flag = DESCRIBE_FLAG.get(memory_type)
        if flag is not None:
            assert world[flag] is True

        await ROUND_TRIPS[memory_type](engine, tmp_path)
    finally:
        await engine.stop()
