"""Engine end-to-end: boot -> write -> retrieve -> rebuild -> describe -> stop."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from memspine import Engine
from memspine.core.records import RecordStatus
from memspine.exceptions import MemspineError, RebuildUnavailableError


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    await eng.start()
    yield eng
    await eng.stop()


async def test_boot_write_retrieve_round_trip(engine: Engine) -> None:
    written = await engine.write("the sky is blue", namespace="agent/alice")
    records = await engine.retrieve(namespace="agent/alice")
    assert [r.record_id for r in records] == [written.record_id]
    assert records[0].content == "the sky is blue"
    assert records[0].status is RecordStatus.ACTIVATED


async def test_rebuild_reproduces_identical_read_model(engine: Engine) -> None:
    for i in range(3):
        await engine.write(f"fact {i}", namespace="agent/alice")
    before = {r.record_id: r.model_dump() for r in await engine.retrieve("agent/alice")}

    counts = await engine.rebuild()
    assert counts["records"] == 3
    after = {r.record_id: r.model_dump() for r in await engine.retrieve("agent/alice")}
    assert after == before


async def test_describe_reports_effective_world(engine: Engine) -> None:
    world = engine.describe()
    assert world["profile"] == "simple"
    assert set(world["memories"]["enabled"]) == {"working", "episodic", "semantic"}
    assert world["event_log"] == {
        "mode": "full",
        "compress": False,
        "retention_days": 30,
        "rebuildable": True,
    }
    assert world["projectors"] == ["records", "vectors"]
    assert world["embedding"] == "hash:64"
    assert world["vector"] == "SQLiteVectorStore"
    assert world["runner"] == "inline"


async def test_personal_template_auto_enables_dependencies() -> None:
    eng = Engine(
        template="personal",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    await eng.start()
    try:
        world = eng.describe()
        enabled = set(world["memories"]["enabled"])
        # reflective -> episodic, prospective -> semantic (C1b)
        assert {"reflective", "prospective", "episodic", "semantic"} <= enabled
    finally:
        await eng.stop()


async def test_ephemeral_engine_writes_but_cannot_rebuild() -> None:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        event_log={"mode": "ephemeral"},
        embedding={"provider": "hash"},
    )
    await eng.start()
    try:
        await eng.write("volatile", namespace="agent/bob")
        # projection still works (events flow through, D-45)
        assert len(await eng.retrieve("agent/bob")) == 1
        assert eng.describe()["event_log"]["rebuildable"] is False
        with pytest.raises(RebuildUnavailableError):
            await eng.rebuild()
    finally:
        await eng.stop()


async def test_search_ranks_semantically_related_first(engine: Engine) -> None:
    await engine.write("the sky is blue today", namespace="agent/alice")
    await engine.write("postgres index tuning notes", namespace="agent/alice")
    scored = await engine.search("blue sky", namespace="agent/alice", top_k=5)
    assert scored, "search returned nothing"
    assert scored[0][0].content == "the sky is blue today"


async def test_assemble_places_persona_first_and_marks_boundary(engine: Engine) -> None:
    await engine.set_persona("agent/alice", "I am Alice's assistant")
    await engine.write("sky is blue", namespace="agent/alice", memory_type="semantic")
    await engine.write("we met yesterday", namespace="agent/alice", memory_type="episodic")

    context = await engine.assemble("sky", namespace="agent/alice", budget_tokens=1000)
    assert not context.abstained
    assert context.records[0].source.channel == "persona"
    assert context.boundary_index >= 1  # persona (+facts) form the stable prefix


async def test_working_memory_pages_out_to_episodic() -> None:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"working": {"enabled": True, "policies": {"page_size": 2}}},
    )
    await eng.start()
    try:
        for i in range(4):
            await eng.write(f"turn {i}", namespace="agent/w", memory_type="working")
        working = await eng.retrieve("agent/w", "working")
        episodic = await eng.retrieve("agent/w", "episodic")
        assert len(working) == 2  # hot window bounded (M13.1)
        assert len(episodic) == 2  # overflow paged out, identity preserved
        assert {r.content for r in episodic} == {"turn 0", "turn 1"}
        assert all(r.version == 2 for r in episodic)
    finally:
        await eng.stop()


async def test_sleep_cycle_and_vector_rebuild(engine: Engine) -> None:
    await engine.write("fact one", namespace="agent/alice")
    results = await engine.sleep()
    assert results["consolidate"]["status"] == "noop"
    assert results["event_log_prune"]["status"] == "skipped"  # full mode

    counts = await engine.rebuild()
    assert counts["vectors"] == 1  # vector projection rebuilt by re-embedding
    scored = await engine.search("fact", namespace="agent/alice")
    assert scored and scored[0][0].content == "fact one"


async def test_set_persona_supersedes_in_place(engine: Engine) -> None:
    """Regression: personas used to accumulate — one new pinned record per call."""
    first = await engine.set_persona("agent/alice", "v1 persona")
    second = await engine.set_persona("agent/alice", "v2 persona")
    assert second.record_id == first.record_id  # stable identity
    assert second.version == 2
    assert second.history[0].content == "v1 persona"

    personas = [
        record
        for record in await engine.retrieve("agent/alice", "working")
        if record.source.channel == "persona"
    ]
    assert len(personas) == 1 and personas[0].content == "v2 persona"

    context = await engine.assemble("anything", namespace="agent/alice", budget_tokens=1000)
    persona_blocks = [r for r in context.records if r.source.channel == "persona"]
    assert len(persona_blocks) == 1


async def test_forget_removes_from_reads_and_survives_rebuild(engine: Engine) -> None:
    kept = await engine.write("keep me", namespace="agent/alice")
    doomed = await engine.write("forget me", namespace="agent/alice")
    await engine.forget(doomed.record_id, namespace="agent/alice")

    remaining = await engine.retrieve("agent/alice")
    assert [record.record_id for record in remaining] == [kept.record_id]
    hits = await engine.search("forget", namespace="agent/alice")
    assert all(record.record_id != doomed.record_id for record, _ in hits)

    await engine.rebuild()  # FORGET must replay identically (D0.1)
    remaining = await engine.retrieve("agent/alice")
    assert [record.record_id for record in remaining] == [kept.record_id]


async def test_search_updates_access_stats_through_the_log(engine: Engine) -> None:
    """M1 reinforcement is event-sourced: RETRIEVE events update access stats."""
    written = await engine.write("the sky is blue", namespace="agent/alice")
    assert written.scoring.access_count == 0

    await engine.search("sky", namespace="agent/alice")
    await engine.search("sky", namespace="agent/alice")

    [record] = await engine.retrieve("agent/alice")
    # 2 searches; the 2nd search's stats-read happens before its own event.
    assert record.scoring.access_count == 2
    assert record.scoring.last_accessed_at is not None


async def test_failed_start_leaks_nothing() -> None:
    """Regression: a mid-start ConfigError used to leak connected clients."""
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        llm={"roles": {"chat": {"provider": "bogus"}}},
    )
    with pytest.raises(Exception, match="bogus"):
        await eng.start()
    assert eng._client is None or not await eng._client.health()
    assert eng._http is None or not await eng._http.health()


async def test_semantic_pipeline_dedups_and_resolves_conflicts(engine: Engine) -> None:
    """P2 end-to-end: semantic writes run dedup + conflict through the engine."""
    first = await engine.write(
        "alice prefers her coffee black in the morning", namespace="agent/p2"
    )
    dup = await engine.write(
        "alice prefers her coffee black in the morning !", namespace="agent/p2"
    )
    assert dup.record_id == first.record_id  # merged, not duplicated
    assert len(await engine.retrieve("agent/p2")) == 1
    assert engine.describe()["semantic_pipeline"] is True
    assert engine.describe()["prompts"]["extract"] == "extract@1"


async def test_semantic_pipeline_survives_rebuild(engine: Engine) -> None:
    await engine.write("alice prefers her coffee black", namespace="agent/p2")
    before = {r.record_id: r.model_dump() for r in await engine.retrieve("agent/p2")}
    await engine.rebuild()
    after = {r.record_id: r.model_dump() for r in await engine.retrieve("agent/p2")}
    # RETRIEVE stats events replay too, so the read model is identical.
    assert after == before


async def test_verbs_require_start() -> None:
    eng = Engine(template="base", dotenv_path=None)
    with pytest.raises(MemspineError, match="not started"):
        await eng.write("x")
    with pytest.raises(MemspineError, match="not started"):
        eng.describe()


def test_sync_wrappers_round_trip() -> None:
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    eng.start_sync()
    try:
        eng.write_sync("hello", namespace="agent/sync")
        assert len(eng.retrieve_sync(namespace="agent/sync")) == 1
    finally:
        eng.stop_sync()


async def test_sync_wrapper_refuses_running_loop() -> None:
    """asyncio.run-style wrappers must fail loudly inside a live event loop."""
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    with pytest.raises(MemspineError, match="running event loop"):
        eng.start_sync()


async def test_describe_requires_started_even_after_stop() -> None:
    """Regression: describe() used to report a healthy world after stop()."""
    eng = Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
    )
    await eng.start()
    assert eng.describe()["profile"] == "simple"
    await eng.stop()
    with pytest.raises(MemspineError, match="not started"):
        eng.describe()


async def test_concurrent_writes_do_not_race_offsets(engine: Engine) -> None:
    """Regression: UPDATE-then-INSERT offsets raced under concurrent writes."""
    import asyncio

    await asyncio.gather(*(engine.write(f"c{i}", namespace="agent/con") for i in range(8)))
    records = await engine.retrieve("agent/con")
    assert len(records) == 8


async def test_strict_services_hard_fails_with_extra_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """D-10: missing required service names the extra; escape hatch degrades."""
    from memspine.core import registry
    from memspine.exceptions import MissingServiceError

    monkeypatch.setitem(registry.REQUIRED_SERVICES, "associative", frozenset({"graph"}))
    cfg = {
        "memories": {"associative": {"enabled": True}},
        "storage": {"path": ":memory:"},
        "embedding": {"provider": "hash"},
    }

    strict = Engine(template="base", dotenv_path=None, user_config=cfg)
    with pytest.raises(MissingServiceError, match=r"memspine\[graph\]"):
        await strict.start()

    lax = Engine(template="base", dotenv_path=None, user_config=cfg, strict_services=False)
    await lax.start()  # degrades with a warning instead
    try:
        assert "associative" in lax.describe()["memories"]["enabled"]
    finally:
        await lax.stop()


async def test_rolling_engine_prunes_on_start(tmp_path: object) -> None:
    """D-45: retention_days is real — a rolling engine prunes applied history."""
    from pathlib import Path

    db = str(Path(str(tmp_path)) / "roll.db")
    cfg = {
        "storage": {"path": db},
        "event_log": {"mode": "rolling", "retention_days": 1},
        "embedding": {"provider": "hash"},
    }
    eng = Engine(template="base", dotenv_path=None, user_config=cfg)
    await eng.start()
    await eng.write("old fact", namespace="agent/roll")
    await eng.stop()

    # Age the event past the retention window by rewriting its timestamp.
    import sqlite3
    from datetime import UTC, datetime, timedelta

    old = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE memory_events SET ts = ?", (old,))

    eng2 = Engine(template="base", dotenv_path=None, user_config=cfg)
    await eng2.start()  # boot-time prune fires
    try:
        import sqlite3 as s3

        with s3.connect(db) as conn:
            remaining = conn.execute("SELECT COUNT(*) FROM memory_events").fetchone()[0]
        assert remaining == 0  # applied + aged event was pruned
        # ... but the projection (the durable read model) is intact:
        assert len(await eng2.retrieve("agent/roll")) == 1
    finally:
        await eng2.stop()
