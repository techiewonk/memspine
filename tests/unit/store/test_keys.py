"""F-01: graph key functions and the flag-on extract_graph key behaviour."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.derive.pipeline.stages.extract_graph import extract_graph
from memspine.prompts.models import ExtractedEdge
from memspine.store import keys
from memspine.store.records import MemoryRecord, RecordStatus

T0 = datetime(2023, 5, 1, tzinfo=UTC)


def test_base_is_normalised_and_scoped() -> None:
    a = keys.base_key("ns", "state", "Alice", "lives_in", "Paris")
    assert a == keys.base_key("ns", "state", "  alice ", "LIVES_IN", "paris")
    assert a != keys.base_key("other", "state", "Alice", "lives_in", "Paris")
    assert a != keys.base_key("ns", "event", "Alice", "lives_in", "Paris")


def test_key_deterministic_and_occurrence_sensitive() -> None:
    base = keys.base_key("ns", "state", "a", "r", "b")
    o1, o2 = keys.occurrence_of(["t1", "t2"]), keys.occurrence_of(["t3"])
    assert keys.occurrence_of(["t2", "t1", "t1"]) == o1
    assert keys.versioned_key(base, o1) == keys.versioned_key(base, o1)
    assert keys.versioned_key(base, o1) != keys.versioned_key(base, o2)
    with pytest.raises(ValueError):
        keys.occurrence_of([])


def test_index_decisions() -> None:
    base = keys.base_key("ns", "state", "a", "r", "b")
    o1, o2 = keys.occurrence_of(["t1"]), keys.occurrence_of(["t2"])
    idx = keys.EdgeKeyIndex()
    assert idx.decide(base, o1).outcome == "new"
    idx.add("r1", True, key=keys.versioned_key(base, o1), base=base)
    assert idx.decide(base, o1).via == "key"
    assert idx.decide(base, o2).via == "base"  # live sibling: provenance
    dead = keys.EdgeKeyIndex()
    dead.add("r1", False, key=keys.versioned_key(base, o1), base=base)
    assert dead.decide(base, o2).outcome == "new_occurrence"  # A-B-A
    legacy = keys.EdgeKeyIndex()
    legacy.add("old", True, alias="v1key")
    assert legacy.decide(base, o1, alias_keys=("v1key",)).via == "alias"


# ---------------------------------------------------------------- engine, flag on / off


def _engine(key_v2: bool, path: str = ":memory:") -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": path, "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={
            "semantic": {"enabled": True},
            "episodic": {"enabled": True},
            "associative": {"enabled": True},
        },
        graph={"key_v2": key_v2},
    )


@pytest.fixture(params=[True, False], ids=["v2", "v1"])
async def engine(request: pytest.FixtureRequest) -> AsyncIterator[Engine]:
    eng = _engine(request.param)
    await eng.start()
    yield eng
    await eng.stop()


def _edge(dst: str) -> ExtractedEdge:
    return ExtractedEdge(
        src_entity="Alice",
        rel="lives_in",
        dst_entity=dst,
        kind="state",
        fact=f"Alice lives in {dst}",
        confidence=0.9,
    )


TABLE = {"moved to Paris": "Paris", "moved to London": "London", "back to Paris": "Paris"}


def _wire(eng: Engine) -> None:
    async def fake(content: str, _ctx: object = None) -> list[ExtractedEdge]:
        return [_edge(TABLE[content])] if content in TABLE else []

    eng._extract_edges = fake


async def _turn(eng: Engine, text: str, day: int, ns: str = "a") -> MemoryRecord:
    return await eng.write(
        text, namespace=ns, memory_type="episodic", valid_from=T0 + timedelta(days=day)
    )


async def _facts(eng: Engine, ns: str = "a") -> list[MemoryRecord]:
    st = eng._require_started()
    return [r for r in await st.list_records(ns, "semantic") if r.source.channel == "extract_graph"]


async def test_a_b_a(engine: Engine) -> None:
    v2 = engine.config.graph.key_v2
    _wire(engine)
    await _turn(engine, "moved to Paris", 0)
    await extract_graph(engine._pipeline_ctx())
    await _turn(engine, "moved to London", 10)
    await extract_graph(engine._pipeline_ctx())
    await _turn(engine, "back to Paris", 20)
    stats = await extract_graph(engine._pipeline_ctx())
    paris = [f for f in await _facts(engine) if f.content == "Alice lives in Paris"]
    if v2:
        assert stats["edges_written"] == 1
        assert len(paris) == 2
        assert sum(f.status is RecordStatus.ACTIVATED for f in paris) == 1
        assert len({f.source.message_id for f in paris}) == 2
    else:
        # flag off: the returning value is swallowed as a duplicate of the closed one
        assert stats["edges_written"] == 0 and len(paris) == 1


async def test_replay_is_idempotent_and_deterministic() -> None:
    keysets = []
    for _ in range(2):
        eng = _engine(True)
        await eng.start()
        _wire(eng)
        await _turn(eng, "moved to Paris", 0)
        await extract_graph(eng._pipeline_ctx())
        again = await extract_graph(eng._pipeline_ctx())
        assert again["edges_written"] == 0
        keysets.append({f.source.message_id for f in await _facts(eng)})
        await eng.stop()
    assert keysets[0] == keysets[1] and len(keysets[0]) == 1


async def test_alias_lookup_finds_a_pre_flag_record(tmp_path) -> None:
    """A record written under the v1 key (flag off) is still a duplicate once the flag is on."""
    database = str(tmp_path / "pre-flag.db")
    first = _engine(False, database)
    await first.start()
    _wire(first)
    await _turn(first, "moved to Paris", 0)
    await extract_graph(first._pipeline_ctx())
    [legacy] = await _facts(first)
    assert not any(t.startswith(keys.BASE_TAG_PREFIX) for t in legacy.tags)
    await first.stop()

    resumed = _engine(True, database)
    await resumed.start()
    _wire(resumed)
    try:
        await _turn(resumed, "back to Paris", 5)
        stats = await extract_graph(resumed._pipeline_ctx())
        assert stats["edges_written"] == 0 and stats["skipped_existing"] == 1
    finally:
        await resumed.stop()


async def test_scope_separation() -> None:
    eng = _engine(True)
    await eng.start()
    _wire(eng)
    await _turn(eng, "moved to Paris", 0, ns="a")
    await _turn(eng, "back to Paris", 0, ns="b")
    stats = await extract_graph(eng._pipeline_ctx())
    assert stats["edges_written"] == 2
    ka = {f.source.message_id for f in await _facts(eng, "a")}
    kb = {f.source.message_id for f in await _facts(eng, "b")}
    assert ka.isdisjoint(kb)
    await eng.stop()
