"""FU-SLEEP: namespace-scoped sleep stages (``offline.sleep_scoped``), the per-namespace sleep cost
pool, ledger persistence (``economy.ledger_persist``) and the Postgres lease table (model-free: a
counting stub miner, the hash embedder; the Postgres tests need ``MEMSPINE_TEST_PG_URL``)."""

from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import EconomyConfig, OfflineConfig
from memspine.derive.job import LeaseStore, is_pg_url, pg_sql
from memspine.derive.pipeline.stages.context import PipelineContext, sweep_namespaces
from memspine.economy import ledger as costs
from memspine.economy.ledger import LedgerStore, WriteCostLedger, cost_scope
from memspine.prompts.models import ExtractedFact

PG_URL = os.environ.get("MEMSPINE_TEST_PG_URL")


@pytest.fixture(autouse=True)
def _no_leak() -> Any:
    yield
    assert costs.installed() is None  # every engine uninstalls its ledger on stop


# ----------------------------------------------------------------------------- flags


def test_flags_default_off() -> None:
    assert OfflineConfig().sleep_scoped is False
    assert EconomyConfig().ledger_persist is False


# ----------------------------------------------------------------------------- scoped sweep


def _engine(path: Path, *, offline: dict[str, Any] | None = None, **extra: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": str(path / "m.db")},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": {"consolidation": {"mine_facts": True}}},
            "semantic": {"enabled": True},
        },
        offline={"sleep_v2": True, "sleep_manual_only": True, **(offline or {})},
        **extra,
    )


class Miner:
    """A stub fact miner: counts calls per namespace (told apart by the speaker in the text) and
    reports each call to the cost ledger, as the LLM router would."""

    def __init__(self) -> None:
        self.calls: dict[str, int] = {"a": 0, "b": 0}

    async def __call__(self, text: str) -> list[ExtractedFact]:
        who = "a" if "Ilse" in text else "b"
        self.calls[who] += 1
        costs.record_call("generative", calls=1, tokens_in=10, tokens_out=5)
        return [ExtractedFact(entity=who, attribute="said", value=f"fact for {who}")]


async def _seed(eng: Engine) -> None:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    for ns, speakers in (("a", ("Ilse", "Tomasz")), ("b", ("Bram", "Noor"))):
        msgs = [
            {
                "role": "user",
                "content": f"{speakers[i % 2]}: line {i} in {ns}",
                "timestamp": (t0 + timedelta(minutes=i)).isoformat(),
            }
            for i in range(3)
        ]
        await eng.ingest_messages(msgs, namespace=ns, session_id=f"s-{ns}", group_id=f"s-{ns}")


async def _mined(eng: Engine, ns: str) -> int:
    rows = await eng.retrieve(namespace=ns, memory_type="semantic")
    return sum(1 for r in rows if "atomic_fact" in r.tags)


async def _run(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, scoped: bool, who: str | None
) -> tuple[Miner, dict[str, int], dict[str, Any]]:
    eng = _engine(
        tmp_path,
        offline={"sleep_scoped": scoped},
        economy={"ledger_v2": True},
    )
    miner = Miner()
    monkeypatch.setattr(eng, "_build_fact_miner", lambda: miner)
    await eng.start()
    try:
        await _seed(eng)
        if who is None:
            await (await eng.sleep()).wait(timeout=60)  # manual: the "*" key
        else:
            assert eng._derive_sched is not None
            await eng._derive_sched.sleep(who).wait(timeout=60)
        mined = {ns: await _mined(eng, ns) for ns in ("a", "b")}
        return miner, mined, eng.cost_report()
    finally:
        await eng.stop()


async def test_scoped_job_touches_only_its_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    miner, mined, report = await _run(monkeypatch, tmp_path, scoped=True, who="a")
    assert miner.calls == {"a": 1, "b": 0}
    assert mined == {"a": 1, "b": 0}
    # the cost sits under the real namespace, not the engine-wide pool
    by_ns = report["by_namespace"]
    assert by_ns["a"]["model_calls"] == 1
    assert by_ns.get("-", {}).get("model_calls", 0) == 0


async def test_scoped_second_namespace_then_runs_alone(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = _engine(tmp_path, offline={"sleep_scoped": True}, economy={"ledger_v2": True})
    miner = Miner()
    monkeypatch.setattr(eng, "_build_fact_miner", lambda: miner)
    await eng.start()
    try:
        await _seed(eng)
        assert eng._derive_sched is not None
        await eng._derive_sched.sleep("a").wait(timeout=60)
        await eng._derive_sched.sleep("b").wait(timeout=60)
        assert miner.calls == {"a": 1, "b": 1}
        assert eng.cost_report("a")["model_calls"] == 1
        assert eng.cost_report("b")["model_calls"] == 1
        assert eng.cost_report()["by_namespace"].get("-", {}).get("model_calls", 0) == 0
    finally:
        await eng.stop()


async def test_manual_sleep_stays_engine_wide(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    miner, mined, report = await _run(monkeypatch, tmp_path, scoped=True, who=None)
    assert miner.calls == {"a": 1, "b": 1}
    assert mined == {"a": 1, "b": 1}
    by_ns = report["by_namespace"]
    assert by_ns["-"]["model_calls"] == 2  # engine-wide: the shared pool
    assert by_ns.get("a", {}).get("model_calls", 0) == 0


async def test_flag_off_a_namespace_job_still_sweeps_everything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    miner, mined, report = await _run(monkeypatch, tmp_path, scoped=False, who="a")
    assert miner.calls == {"a": 1, "b": 1}
    assert mined == {"a": 1, "b": 1}
    assert report["by_namespace"]["-"]["model_calls"] == 2


async def test_budget_applies_per_namespace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    eng = _engine(
        tmp_path,
        offline={"sleep_scoped": True},
        economy={"ledger_v2": True, "budget": {"max_calls": 1}},
        workers={"outbox": True},
    )
    miner = Miner()
    monkeypatch.setattr(eng, "_build_fact_miner", lambda: miner)
    await eng.start()
    try:
        await _seed(eng)
        assert eng._derive_sched is not None
        stats_a = await eng._derive_sched.sleep("a").wait(timeout=60)
        # a's one call is spent: its later stages are held back, not dropped
        assert stats_a["mine_facts"]["status"] == "ok"
        assert any(s.get("status") == "deferred_budget" for s in stats_a.values())
        stats_b = await eng._derive_sched.sleep("b").wait(timeout=60)
        assert stats_b["mine_facts"]["status"] == "ok"  # b has its own budget
        budget = eng.cost_report()["budget"]["namespaces"]
        assert budget["a"]["calls"] == 1 and budget["b"]["calls"] == 1
    finally:
        await eng.stop()


async def test_sweep_namespaces_helper() -> None:
    class Storage:
        async def list_namespaces(self) -> list[str]:
            return ["a", "b"]

    ctx = PipelineContext(storage=Storage(), config=None)  # type: ignore[arg-type]
    assert await sweep_namespaces(ctx) == ["a", "b"]
    ctx.scope_namespace = "b"
    assert await sweep_namespaces(ctx) == ["b"]
    ctx.scope_namespace = "zz"
    assert await sweep_namespaces(ctx) == []


# ----------------------------------------------------------------------------- ledger persistence


def test_ledger_store_survives_a_restart(tmp_path: Path) -> None:
    db = str(tmp_path / "l.cost")
    first = WriteCostLedger(store=LedgerStore(db))
    with cost_scope(namespace="Ilse-ns", layer="semantic", stage="mine_facts"):
        first.record("generative", model="m", calls=2, tokens_in=30, tokens_out=7, latency_ms=12.5)
    with cost_scope(namespace="Bram-ns", stage="window_extract"):
        first.record("embedding", calls=4)
    first.record("generative", calls=1, tokens_in=1, scope=costs.CostScope("-", "-", "consolidate"))
    want = first.report()
    first.close()

    second = WriteCostLedger(store=LedgerStore(db))
    got = second.report()
    for key in ("model_calls", "embed_calls", "tokens_in", "tokens_out", "latency_ms"):
        assert got[key] == want[key], key
    assert second.report("Ilse-ns")["model_calls"] == 2  # a name matches its reloaded hash
    assert second.report("Bram-ns")["embed_calls"] == 4
    assert set(got["by_stage"]) == set(want["by_stage"])
    # new calls add to the reloaded totals, and name the namespace again
    with cost_scope(namespace="Ilse-ns", layer="semantic", stage="mine_facts"):
        second.record("generative", calls=1)
    assert second.report()["model_calls"] == want["model_calls"] + 1
    assert "Ilse-ns" in second.report()["by_namespace"]
    second.close()
    # the file keeps hashes, never the name
    with sqlite3.connect(db) as con:
        names = {r[0] for r in con.execute("SELECT ns_hash FROM cost_entry")}
    assert "Ilse-ns" not in names and "Bram-ns" not in names and "-" in names


async def test_engine_cost_report_survives_restart(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    async def run(persist: bool) -> dict[str, Any]:
        eng = _engine(
            tmp_path,
            offline={"sleep_scoped": True},
            economy={"ledger_v2": True, "ledger_persist": persist},
        )
        miner = Miner()
        monkeypatch.setattr(eng, "_build_fact_miner", lambda: miner)
        await eng.start()
        try:
            before = eng.cost_report()
            if not before.get("model_calls"):  # first run only
                await _seed(eng)
                assert eng._derive_sched is not None
                await eng._derive_sched.sleep("a").wait(timeout=60)
            return {"before": before, "after": eng.cost_report()}
        finally:
            await eng.stop()

    first = await run(True)
    assert first["after"]["model_calls"] == 1
    assert (tmp_path / "m.db.cost").exists()
    again = await run(True)  # a fresh engine on the same files
    assert again["before"]["model_calls"] == 1
    assert again["before"]["tokens_in"] == first["after"]["tokens_in"] > 10
    assert again["before"]["by_stage"].keys() == first["after"]["by_stage"].keys()


async def test_ledger_persist_off_writes_no_file_and_starts_empty(tmp_path: Path) -> None:
    eng = _engine(tmp_path, economy={"ledger_v2": True})
    await eng.start()
    try:
        with cost_scope(namespace="a", stage="x"):
            costs.record_call("generative", calls=1)
        assert eng.cost_report()["model_calls"] == 1
    finally:
        await eng.stop()
    assert not (tmp_path / "m.db.cost").exists()
    eng = _engine(tmp_path, economy={"ledger_v2": True})
    await eng.start()
    try:
        assert eng.cost_report()["model_calls"] == 0
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- Postgres leases


def test_pg_sql_translation() -> None:
    assert is_pg_url("postgresql://u:p@h/db") and is_pg_url("postgresql+psycopg://h/d")
    assert not is_pg_url("/tmp/x.sleep") and not is_pg_url(":memory:")
    assert pg_sql("SELECT epoch FROM sleep_lease WHERE ns=? AND epoch=?") == (
        "SELECT epoch FROM sleep_lease WHERE ns=%s AND epoch=%s"
    )
    ignore = pg_sql("INSERT OR IGNORE INTO sleep_state (ns) VALUES (?)")
    assert ignore == "INSERT INTO sleep_state (ns) VALUES (%s) ON CONFLICT DO NOTHING"
    replace = pg_sql(
        "INSERT OR REPLACE INTO sleep_watermark (ns, kind, cycle, stage, seq, done_at) "
        "VALUES (?,?,?,?,?,?)"
    )
    assert "ON CONFLICT (ns, kind, cycle, stage) DO UPDATE SET seq=EXCLUDED.seq" in replace
    assert "OR REPLACE" not in replace and replace.count("%s") == 6
    assert "GREATEST(pending,1)" in pg_sql("UPDATE d SET pending=MAX(pending,1) WHERE ns=?")
    ddl = pg_sql("id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL")
    assert "BIGSERIAL PRIMARY KEY" in ddl and "DOUBLE PRECISION" in ddl


def test_postgres_storage_url_is_the_lease_path(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from memspine.derive.pipeline import scheduler as sched

    seen: list[str] = []

    class Fake(LeaseStore):
        def __init__(self, path: str = ":memory:", **kw: Any) -> None:
            seen.append(path)
            super().__init__(":memory:", **kw)

    monkeypatch.setattr(sched, "LeaseStore", Fake)
    off = OfflineConfig()
    cfg = SimpleNamespace(
        offline=off,
        storage=SimpleNamespace(backend="postgres", url="postgresql://u@h/db", path="x"),
        runtime=SimpleNamespace(scheduler_v2=False),
    )
    sched.build_sleep_v2(SimpleNamespace(), cfg)
    assert seen == ["postgresql://u@h/db"]


@pytest.fixture
def pg_store() -> Any:
    if not PG_URL:
        pytest.skip("Postgres leases: set MEMSPINE_TEST_PG_URL to a scratch Postgres DSN")
    pytest.importorskip("psycopg")
    clock = _Clock()
    store = LeaseStore(PG_URL, clock=clock)
    store.clock = clock  # type: ignore[assignment]
    yield store, clock


class _Clock:
    def __init__(self) -> None:
        self.t = 1_800_000_000.0

    def __call__(self) -> float:
        return self.t


def test_pg_lease_claim_fence_epoch_and_watermarks(pg_store: Any) -> None:
    store, clock = pg_store
    ns = f"fu-{uuid.uuid4().hex[:10]}"
    other = LeaseStore(PG_URL, clock=clock)  # a second process on the same database
    one = store.claim(ns, "p1", "job-1", 30.0)
    assert one is not None and one.epoch == 1
    assert other.claim(ns, "p2", "job-2", 30.0) is None  # fenced across connections
    assert other.get_lease(ns).job_id == "job-1"  # type: ignore[union-attr]
    cycle, resumed = store.open_cycle(one, "sleep")  # type: ignore[misc]
    assert (cycle, resumed) == (1, False)
    assert store.mark_stage(one, "sleep", cycle, "consolidate", 7) is not None
    assert store.mark_stage(one, "sleep", cycle, "consolidate", 9) is not None  # replace
    marks = other.watermarks(ns, "sleep", cycle)
    assert [(w.stage, w.seq) for w in marks] == [("consolidate", 9)]
    clock.t += 60  # the lease expires; p2 takes over with a higher epoch
    two = other.claim(ns, "p2", "job-2", 30.0)
    assert two is not None and two.epoch == 2
    assert store.mark_stage(one, "sleep", cycle, "mine_facts", 1) is None  # stale holder fenced
    assert (ns, "mark_stage", 1, 2) in other.fence_log()
    assert other.open_cycle(two, "sleep") == (1, True)  # the open cycle resumes
    assert other.defer_stage(two, "sleep", 1, "mine_facts")
    assert other.defer_stage(two, "sleep", 1, "mine_facts")  # same cycle: counted once
    assert store.deferred(ns)["mine_facts"] == {"pending": 1, "total": 1, "last_cycle": 1}
    assert other.finish_cycle(two, "sleep", 1, full=True)
    other.bump_deposits(ns, 3)
    other.carry_deferred(ns, 1)
    assert store.state(ns)["deposits_since_run"] == 4  # type: ignore[index]
    assert other.release(two) and other.get_lease(ns).lease_until == 0  # type: ignore[union-attr]


class _Crash(BaseException):
    """A simulated process death (nothing is cleaned up)."""


async def test_pg_namespace_job_resumes_after_a_crash(pg_store: Any) -> None:
    from memspine.derive.job import NamespaceJob

    store, clock = pg_store
    ns = f"fu-{uuid.uuid4().hex[:10]}"
    ran: list[str] = []

    async def run(stage: str) -> dict[str, object]:
        if stage == "mine_facts" and "boom" not in ran:
            ran.append("boom")
            raise _Crash
        ran.append(stage)
        return {"status": "ok"}

    stages = ["consolidate", "mine_facts", "decay_sweep"]

    def job(name: str) -> NamespaceJob:
        return NamespaceJob(store, ns, stages, run, owner=name, job_id=name, ttl=30.0)

    with pytest.raises(_Crash):
        await job("j1").run()
    clock.t += 60  # the dead holder's lease expires
    out = await job("j2").run()
    assert out.state == "succeeded" and out.resumed and out.skipped == ["consolidate"]
    assert ran == ["consolidate", "boom", "mine_facts", "decay_sweep"]
