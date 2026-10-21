"""RM-74 / RM-78: Engine warm-up after start, readiness warm state and registry scope."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.models.encoders.embedding.hash_local import HashEmbedding
from memspine.providers.shared import resource_key, shared_registry
from memspine.runtime.warmup import WarmState, WarmupError


class _Gate:
    """A fake slow embedder warm hook: waits until released (or forever)."""

    def __init__(self) -> None:
        self.release = asyncio.Event()
        self.started = 0
        self.finished = 0
        self.cancelled = 0
        self.error: Exception | None = None

    async def warm(self) -> None:
        self.started += 1
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        if self.error is not None:
            raise self.error
        self.finished += 1


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> _Gate:
    g = _Gate()

    async def warm(_self: HashEmbedding) -> None:
        await g.warm()

    monkeypatch.setattr(HashEmbedding, "warm", warm, raising=False)
    return g


def _engine(path: str = ":memory:", **shared: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": path, "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        runtime={"shared_resources": shared},
    )


async def test_background_start_returns_before_warm_then_reports_warm(gate: _Gate) -> None:
    eng = await asyncio.wait_for(_engine(warmup="background").start(), 30)
    try:
        assert eng.warm_state()["embedder"] == WarmState("embedder", "warming")
        gate.release.set()
        states = await eng.wait_until_warm(5.0)
        assert states["embedder"] == WarmState("embedder", "warm") and gate.finished == 1
        report = await eng.readiness()
        assert report.warm["embedder"].status == "warm"
        assert report.ready  # warm state never gates readiness
    finally:
        await eng.stop()


async def test_requests_are_served_while_warming(gate: _Gate) -> None:
    eng = await _engine().start()
    try:
        rec = await eng.write("Mira lives in Lisbon.")
        assert rec.record_id
        assert eng.warm_state()["embedder"].status == "warming"
    finally:
        gate.release.set()
        await eng.stop()


async def test_blocking_start_waits_for_warm(gate: _Gate) -> None:
    gate.release.set()
    eng = await _engine(warmup="blocking").start()
    try:
        assert gate.finished == 1
        assert eng.warm_state()["embedder"].status == "warm"
    finally:
        await eng.stop()


async def test_blocking_timeout_fails_start_naming_the_category(gate: _Gate) -> None:
    eng = _engine(warmup="blocking", warmup_timeout_s=0.05)
    with pytest.raises(WarmupError, match="failed for embedder"):
        await eng.start()
    assert gate.cancelled == 1
    assert not eng._started  # start tore everything down


async def test_background_failure_is_reported_not_raised(gate: _Gate) -> None:
    gate.error = OSError("weights missing")
    gate.release.set()
    eng = await _engine().start()
    try:
        states = await eng.wait_until_warm(5.0)
        assert states["embedder"] == WarmState("embedder", "failed", "OSError")
        assert (await eng.readiness()).warm["embedder"].error_type == "OSError"
    finally:
        await eng.stop()


async def test_lazy_warms_nothing(gate: _Gate) -> None:
    eng = await _engine(warmup="lazy").start()
    try:
        await asyncio.sleep(0.01)
        assert gate.started == 0
        assert eng.warm_state()["embedder"].status == "not_requested"
    finally:
        await eng.stop()


async def test_excluded_category_is_not_requested(gate: _Gate) -> None:
    eng = await _engine(warmup_exclude=["embedder"]).start()
    try:
        assert eng.warm_state()["embedder"].status == "not_requested"
    finally:
        await eng.stop()
    assert gate.started == 0


async def test_stop_cancels_a_pending_background_warmup(gate: _Gate) -> None:
    eng = await _engine().start()
    for _ in range(100):
        if gate.started:
            break
        await asyncio.sleep(0.01)
    assert gate.started == 1
    await asyncio.wait_for(eng.stop(), 10)  # never hangs on the blocked warm-up
    assert gate.cancelled == 1
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("memspine-warm")]
    assert eng.warm_state() == eng.warm_state()  # still readable after stop


async def test_scope_engine_uses_a_private_registry(tmp_path: Path, gate: _Gate) -> None:
    gate.release.set()
    db = str(tmp_path / "m.db")
    a = await _engine(db, scope="engine").start()
    b = await _engine(db, scope="engine").start()
    try:
        assert a._resource_registry is not shared_registry()
        assert a._resource_registry is not b._resource_registry
        assert a._sql is not None and b._sql is not None
        assert a._sql.engine is not b._sql.engine  # nothing shared across Engines
    finally:
        await a.stop()
        await b.stop()


async def test_scope_process_shares_the_sql_pool(tmp_path: Path, gate: _Gate) -> None:
    gate.release.set()
    db = str(tmp_path / "m.db")
    a = await _engine(db).start()
    b = await _engine(db).start()
    try:
        assert a._resource_registry is shared_registry() is b._resource_registry
        assert a._sql is not None and b._sql is not None
        assert a._sql.engine is b._sql.engine
        assert a._sql.engine.pool.size() == 5  # runtime.shared_resources.sql.pool_size
    finally:
        await a.stop()
        await b.stop()


async def test_sql_pool_settings_apply_to_file_sqlite(tmp_path: Path, gate: _Gate) -> None:
    gate.release.set()
    eng = await _engine(
        str(tmp_path / "p.db"), sql={"pool_size": 2, "max_overflow": 0, "pool_recycle_s": 60}
    ).start()
    try:
        assert eng._sql is not None
        pool: Any = eng._sql.engine.pool
        assert pool.size() == 2 and pool._max_overflow == 0 and pool._recycle == 60
        assert pool._pre_ping is True
    finally:
        await eng.stop()


async def test_private_registry_closes_retained_weights_at_stop(gate: _Gate) -> None:
    gate.release.set()
    eng = await _engine(scope="engine").start()
    registry = eng._resource_registry
    assert registry is not None
    closed: list[str] = []

    async def build() -> str:
        return "weights"

    async def close(value: str) -> None:
        closed.append(value)

    lease = await registry.acquire(
        resource_key("model.fake", {}, scope="thread"), build, close=close, kind="weights"
    )
    await lease.aclose()
    assert closed == [] and registry.stats()[0].idle  # keep_warm: retained
    await eng.stop()
    assert closed == ["weights"]  # a private registry frees what it retained


async def test_process_registry_policy_is_withdrawn_at_stop(gate: _Gate) -> None:
    gate.release.set()
    before = len(shared_registry()._policies)
    eng = await _engine().start()
    assert len(shared_registry()._policies) == before + 1
    assert shared_registry().retention.keep_warm
    await eng.stop()
    assert len(shared_registry()._policies) == before


async def test_configured_lexical_index_is_warmed(tmp_path: Path, gate: _Gate) -> None:
    gate.release.set()
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={
            "provider": "sqlite",
            "settings": {"path": str(tmp_path / "lex.db"), "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {}},
        read={"hybrid": True},
    )
    await eng.start()
    try:
        states = await eng.wait_until_warm(10.0)
        assert states["lexical"] == WarmState("lexical", "warm")
        assert "decider" not in states  # not configured: no target, nothing imported
        assert not any(name.startswith("reranker:") for name in states)
    finally:
        await eng.stop()
