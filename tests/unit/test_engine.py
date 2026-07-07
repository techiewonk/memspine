"""Engine end-to-end: boot -> write -> retrieve -> rebuild -> describe -> stop."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from memspine import Engine
from memspine.core.records import RecordStatus
from memspine.exceptions import MemspineError, RebuildUnavailableError


@pytest.fixture
async def engine() -> AsyncIterator[Engine]:
    eng = Engine(template="base", dotenv_path=None, storage={"path": ":memory:"})
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
    assert world["projectors"] == ["records"]


async def test_personal_template_auto_enables_dependencies() -> None:
    eng = Engine(template="personal", dotenv_path=None, storage={"path": ":memory:"})
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


async def test_verbs_require_start() -> None:
    eng = Engine(template="base", dotenv_path=None)
    with pytest.raises(MemspineError, match="not started"):
        await eng.write("x")
    with pytest.raises(MemspineError, match="not started"):
        eng.describe()


def test_sync_wrappers_round_trip() -> None:
    eng = Engine(template="base", dotenv_path=None, storage={"path": ":memory:"})
    eng.start_sync()
    try:
        eng.write_sync("hello", namespace="agent/sync")
        assert len(eng.retrieve_sync(namespace="agent/sync")) == 1
    finally:
        eng.stop_sync()


async def test_sync_wrapper_refuses_running_loop() -> None:
    """asyncio.run-style wrappers must fail loudly inside a live event loop."""
    eng = Engine(template="base", dotenv_path=None, storage={"path": ":memory:"})
    with pytest.raises(MemspineError, match="running event loop"):
        eng.start_sync()


async def test_describe_requires_started_even_after_stop() -> None:
    """Regression: describe() used to report a healthy world after stop()."""
    eng = Engine(template="base", dotenv_path=None, storage={"path": ":memory:"})
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
    cfg = {"memories": {"associative": {"enabled": True}}, "storage": {"path": ":memory:"}}

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
