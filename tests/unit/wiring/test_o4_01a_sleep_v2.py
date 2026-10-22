"""O4-01a wiring: non-blocking sleep, triggers, namespace jobs, leases, watermarks (model-free)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from memspine import Engine
from memspine.config.schema import OfflineConfig
from memspine.derive.job import (
    LeaseStore,
    NamespaceJob,
    SleepHandle,
    SleepJobError,
)
from memspine.derive.scheduler import DerivationScheduler, in_derive_job
from memspine.derive.triggers import TriggerConfig, TriggerPolicy, TriggerState
from memspine.models.concurrency import Priority, current_priority
from memspine.workers.schedule import SLEEP_CYCLE_ORDER


class Clock:
    def __init__(self, t: float = 1_800_000_000.0) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


class Crash(BaseException):
    """A simulated process death (not an Exception: nothing is cleaned up)."""


def _dt(clock: Clock) -> datetime:
    return datetime.fromtimestamp(clock.t, UTC)


# ----------------------------------------------------------------------------- flag and config


def test_flag_default_off() -> None:
    cfg = OfflineConfig()
    assert cfg.sleep_v2 is False
    assert (cfg.sleep_min_turns, cfg.sleep_idle_minutes) == (20, 10.0)
    assert (cfg.sleep_context_pressure, cfg.sleep_min_gap_minutes) == (0.70, 60.0)


def _engine(path: Path, **offline: object) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "provider": "sqlite",
            "settings": {"path": str(path / "m.db"), "busy_timeout_s": 5.0},
        },
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
        offline=offline,
    )


async def test_flag_off_sleep_blocks_and_returns_stats(tmp_path: Path) -> None:
    eng = _engine(tmp_path)
    await eng.start()
    try:
        assert eng._derive_sched is None
        await eng.write("Ilse likes tea", namespace="u")
        stats = await eng.sleep()
        assert isinstance(stats, dict) and "consolidate" in stats and "event_log_prune" in stats
        assert not (tmp_path / "m.db.sleep").exists()  # no lease file when the flag is off
    finally:
        await eng.stop()


async def test_flag_on_engine_returns_handle_with_same_stats(tmp_path: Path) -> None:
    (tmp_path / "a").mkdir()
    ref = _engine(tmp_path / "a")
    await ref.start()
    try:
        await ref.write("Ilse likes tea", namespace="u")
        ref_stats = await ref.sleep()
    finally:
        await ref.stop()
    (tmp_path / "b").mkdir()
    eng = _engine(tmp_path / "b", sleep_v2=True, sleep_manual_only=True)
    await eng.start()
    try:
        for i in range(3):
            await eng.write(f"Ilse likes tea {i}", namespace="u")
        sched = eng._derive_sched
        assert sched is not None
        assert sched.store.state("u")["deposits_since_run"] == 3  # type: ignore[index]
        handle = await eng.sleep()
        assert isinstance(handle, SleepHandle)
        assert handle.status().state in ("queued", "running", "succeeded")
        stats = await handle.wait(timeout=60)
        assert handle.status().state == "succeeded"
        assert list(stats) == list(ref_stats)
        assert all(s.get("status") != "error" for s in stats.values())
        # RM-111 WP5: the leases are tables of the storage database, never a side file
        assert not (tmp_path / "b" / "m.db.sleep").exists()
        assert sched.store.offload
        lease = sched.store.get_lease("*")
        assert lease is not None and lease.lease_until == 0  # released
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- trigger arithmetic

POLICY = TriggerPolicy(
    TriggerConfig(
        min_turns=5,
        idle=timedelta(minutes=10),
        context_pressure=0.7,
        min_gap=timedelta(hours=1),
        maintenance_interval=timedelta(hours=24),
    )
)
T0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)


def test_trigger_needs_n_and_m() -> None:
    few = TriggerState(deposits_since_run=4, last_deposit_at=T0 - timedelta(hours=1))
    assert not POLICY.decide(few, T0).fire and POLICY.decide(few, T0).reason == "not_due"
    busy = TriggerState(deposits_since_run=5, last_deposit_at=T0 - timedelta(minutes=3))
    d = POLICY.decide(busy, T0)
    assert not d.fire and d.reason == "idle_wait"
    assert d.next_check_at == T0 - timedelta(minutes=3) + timedelta(minutes=10)
    idle = TriggerState(deposits_since_run=5, last_deposit_at=T0 - timedelta(minutes=10))
    d = POLICY.decide(idle, T0)
    assert d.fire and d.reason == "deposits_idle" and d.kind == "sleep"


def test_trigger_pressure_or_and_gap() -> None:
    pressed = TriggerState(deposits_since_run=0, context_pressure=0.7)
    d = POLICY.decide(pressed, T0)
    assert d.fire and d.reason == "context_pressure"
    both = TriggerState(
        deposits_since_run=9, last_deposit_at=T0 - timedelta(hours=1), context_pressure=0.9
    )
    assert POLICY.decide(both, T0).reason == "deposits_idle+context_pressure"
    gated = TriggerState(
        deposits_since_run=9,
        last_deposit_at=T0 - timedelta(hours=1),
        context_pressure=0.9,
        last_run_at=T0 - timedelta(minutes=20),
    )
    d = POLICY.decide(gated, T0)
    assert not d.fire and d.reason == "gap" and d.next_check_at == T0 + timedelta(minutes=40)
    ok = TriggerState(
        deposits_since_run=9,
        last_deposit_at=T0 - timedelta(hours=2),
        last_run_at=T0 - timedelta(hours=1),
    )
    assert POLICY.decide(ok, T0).fire


def test_trigger_legacy_interval_mapping_and_validation() -> None:
    legacy = TriggerPolicy(
        TriggerConfig(min_turns=0, idle=timedelta(0), min_gap=timedelta(hours=1))
    )
    # N = 0 still needs at least one deposit (nothing to do otherwise)
    assert not legacy.decide(TriggerState(), T0).fire
    assert legacy.decide(TriggerState(deposits_since_run=1, last_deposit_at=T0), T0).fire
    with pytest.raises(ValueError):
        TriggerConfig(context_pressure=0.0)
    with pytest.raises(ValueError):
        TriggerConfig(min_turns=-1)


def test_maintenance_due() -> None:
    assert POLICY.maintenance_due(TriggerState(), T0)
    last = TriggerState(last_maintenance_at=T0 - timedelta(hours=23))
    assert not POLICY.maintenance_due(last, T0)
    assert POLICY.maintenance_due(TriggerState(last_maintenance_at=T0 - timedelta(hours=24)), T0)


# ----------------------------------------------------------------------------- job, lease, fence


def _store(tmp_path: Path, clock: Clock) -> LeaseStore:
    return LeaseStore(str(tmp_path / "leases.db"), clock=clock)


def _job(store: LeaseStore, name: str, stages: list[str], run, **kw) -> NamespaceJob:  # type: ignore[no-untyped-def]
    return NamespaceJob(store, "u", stages, run, owner=f"proc-{name}", job_id=name, ttl=60.0, **kw)


async def test_two_concurrent_sleeps_one_runner_other_fenced(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    gate = asyncio.Event()
    ran: list[tuple[str, str]] = []

    def runner(who: str):  # type: ignore[no-untyped-def]
        async def run(stage: str) -> dict[str, object]:
            ran.append((who, stage))
            await gate.wait()
            return {"status": "ok"}

        return run

    first = asyncio.create_task(_job(store, "a", ["s1", "s2"], runner("a")).run())
    await asyncio.sleep(0.05)  # a holds the lease inside s1
    second = await _job(store, "b", ["s1", "s2"], runner("b")).run()
    assert second.state == "coalesced" and second.holder == "a"
    gate.set()
    done = await first
    assert done.state == "succeeded"
    assert [w for w, _ in ran] == ["a", "a"]  # b never ran a stage
    lease = store.get_lease("u")
    assert lease is not None and lease.epoch == 1 and lease.lease_until == 0


async def test_scheduler_handles_second_sleep_coalesced(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    gate = asyncio.Event()

    async def run_stage(stage: str) -> dict[str, object]:
        await gate.wait()
        return {"status": "ok"}

    sched = DerivationScheduler(
        store=store,
        policy=POLICY,
        run_stage=run_stage,
        stages=lambda: ["s1"],
        maintenance_stages=lambda: [],
        clock=clock,
        idle_timers=False,
    )
    h1 = sched.sleep("u")
    assert h1.status().state == "queued"  # returned before anything ran
    await asyncio.sleep(0.05)
    assert h1.status().state == "running" and not h1.done
    h2 = sched.sleep("u")
    await asyncio.sleep(0.05)
    assert h2.status().state == "coalesced" and h2.status().coalesced_into == h1.job_id
    gate.set()
    assert await h1.wait(timeout=5) == {"s1": {"status": "ok"}}
    assert await h2.wait(timeout=5) == {}
    assert h1.status().state == "succeeded"
    await sched.stop()


async def test_handle_nonblocking_and_cancel(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    started = asyncio.Event()
    gate = asyncio.Event()
    seen: list[str] = []

    async def run_stage(stage: str) -> dict[str, object]:
        seen.append(stage)
        started.set()
        await gate.wait()
        return {"status": "ok"}

    sched = DerivationScheduler(
        store=store,
        policy=POLICY,
        run_stage=run_stage,
        stages=lambda: ["s1", "s2", "s3"],
        maintenance_stages=lambda: [],
        clock=clock,
        idle_timers=False,
    )
    handle = sched.sleep("u")
    assert not handle.done  # non-blocking: nothing has even started
    await started.wait()
    assert handle.cancel() is True
    gate.set()
    with pytest.raises(SleepJobError):
        await handle.wait(timeout=5)
    assert handle.status().state == "cancelled"
    assert seen == ["s1"]  # stopped at the stage boundary
    assert handle.cancel() is False
    # the cycle stays open: the next sleep resumes after s1
    nxt = sched.sleep("u")
    await nxt.wait(timeout=5)
    assert seen == ["s1", "s2", "s3"]
    await sched.stop()


async def test_crash_mid_stage_resumes_at_watermark(tmp_path: Path) -> None:
    clock = Clock()
    stages = ["s1", "s2", "s3", "s4"]
    calls: list[str] = []
    crash_at = {"s3"}

    async def run(stage: str) -> dict[str, object]:
        calls.append(stage)
        if stage in crash_at:
            raise Crash(stage)
        return {"status": "ok"}

    store = _store(tmp_path, clock)
    with pytest.raises(Crash):
        await _job(store, "a", stages, run).run()
    assert calls == ["s1", "s2", "s3"]
    marks = [w.stage for w in store.watermarks("u", "sleep", 1)]
    assert sorted(marks) == ["s1", "s2"]  # s3 was in flight, never marked
    held = store.get_lease("u")
    assert held is not None and held.lease_until > clock.t  # a dead process keeps its lease...

    # ...so an immediate restart is fenced, and runs once the lease has expired
    store2 = _store(tmp_path, clock)  # a fresh process over the same file
    crash_at.clear()
    blocked = await _job(store2, "b", stages, run).run()
    assert blocked.state == "coalesced"
    clock.advance(61)
    resumed = await _job(store2, "c", stages, run).run()
    assert resumed.state == "succeeded" and resumed.resumed is True
    assert resumed.skipped == ["s1", "s2"]
    assert calls == ["s1", "s2", "s3", "s3", "s4"]  # no completed stage repeated
    assert store2.get_lease("u").epoch == 2  # type: ignore[union-attr]
    # the finished cycle starts clean next time: a full pass again
    calls.clear()
    third = await _job(store2, "d", stages, run).run()
    assert third.resumed is False and calls == stages


async def test_failed_stage_releases_lease_and_resumes(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    calls: list[str] = []
    boom = {"on": True}

    async def run(stage: str) -> dict[str, object]:
        calls.append(stage)
        if stage == "s2" and boom["on"]:
            raise RuntimeError("model down")
        return {"status": "ok"}

    out = await _job(store, "a", ["s1", "s2"], run).run()
    assert out.state == "failed" and "model down" in (out.error or "")
    assert store.get_lease("u").lease_until == 0  # type: ignore[union-attr]
    boom["on"] = False
    out2 = await _job(store, "b", ["s1", "s2"], run).run()
    assert out2.state == "succeeded" and out2.skipped == ["s1"]
    assert calls == ["s1", "s2", "s2"]


async def test_expired_lease_is_reclaimed_and_stale_holder_fenced(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    old = store.claim("u", "p1", "j1", ttl=30.0)
    assert old is not None and old.epoch == 1
    assert store.claim("u", "p2", "j2", ttl=30.0) is None  # still held
    clock.advance(31)  # lock fault: the holder died, the lease lapsed
    new = store.claim("u", "p2", "j2", ttl=30.0)
    assert new is not None and new.epoch == 2 and new.owner == "p2"
    # the stale holder wakes up: every write is refused, and the refusal is logged
    cycle = store.open_cycle(new, "sleep")
    assert cycle == (1, False)
    assert store.heartbeat(old, 30.0) is None
    assert store.mark_stage(old, "sleep", 1, "s1") is None
    assert store.finish_cycle(old, "sleep", 1, full=True) is False
    assert store.release(old) is False
    assert store.watermarks("u", "sleep", 1) == []
    assert [(op, pe, ce) for _, op, pe, ce in store.fence_log()] == [
        ("heartbeat", 1, 2),
        ("mark_stage", 1, 2),
        ("finish_cycle", 1, 2),
        ("release", 1, 2),
    ]
    assert store.get_lease("u").owner == "p2"  # type: ignore[union-attr]


async def test_job_loses_lease_mid_run_stops_without_writing(tmp_path: Path) -> None:
    clock = Clock()
    store = _store(tmp_path, clock)
    calls: list[str] = []

    async def run(stage: str) -> dict[str, object]:
        calls.append(stage)
        if stage == "s1":
            clock.advance(61)  # the lease lapses while s1 runs...
            assert store.claim("u", "p2", "thief", ttl=60.0) is not None  # ...and is taken over
        return {"status": "ok"}

    out = await _job(store, "a", ["s1", "s2"], run).run()
    assert out.state == "failed" and "lease lost" in (out.error or "")
    assert calls == ["s1"]
    assert store.watermarks("u", "sleep", 1) == []  # the fenced holder advanced nothing
    assert store.get_lease("u").owner == "p2"  # type: ignore[union-attr]


# ----------------------------------------------------------------------------- scheduler triggers


async def test_commit_counts_and_trigger_fires_job(tmp_path: Path) -> None:
    clock = Clock()
    store = LeaseStore(str(tmp_path / "l.db"), clock=clock)
    ran: list[str] = []

    async def run_stage(stage: str) -> dict[str, object]:
        ran.append(stage)
        return {"status": "ok"}

    cfg = TriggerConfig(min_turns=2, idle=timedelta(minutes=10), min_gap=timedelta(hours=1))
    sched = DerivationScheduler(
        store=store,
        policy=TriggerPolicy(cfg),
        run_stage=run_stage,
        stages=lambda: ["s1", "s2"],
        maintenance_stages=lambda: ["m1"],
        clock=clock,
        idle_timers=False,
    )
    sched.on_commit("u", 1)
    sched.on_commit("u", 2)
    assert store.state("u")["deposits_since_run"] == 2  # type: ignore[index]
    clock.advance(5 * 60)
    d = sched.evaluate("u")
    assert not d.fire and d.reason == "idle_wait"
    await asyncio.sleep(0.05)
    assert ran == []
    clock.advance(6 * 60)
    d = sched.evaluate("u")
    assert d.fire and d.reason == "deposits_idle"
    await asyncio.gather(*sched._tasks)
    assert ran == ["s1", "s2"]
    st = store.state("u")
    assert st["deposits_since_run"] == 0 and st["last_run_at"] == clock.t  # type: ignore[index]
    # new deposits inside the K gap wait it out
    sched.on_commit("u", 3)
    sched.on_commit("u", 4)
    clock.advance(11 * 60)
    d = sched.evaluate("u")
    assert not d.fire and d.reason == "gap"
    await sched.stop()


async def test_writes_inside_a_job_do_not_count(tmp_path: Path) -> None:
    clock = Clock()
    store = LeaseStore(str(tmp_path / "l.db"), clock=clock)
    holder: dict[str, DerivationScheduler] = {}

    async def run_stage(stage: str) -> dict[str, object]:
        assert in_derive_job()
        holder["s"].on_commit("u", 99)  # a derived write made by the stage itself
        return {"status": "ok"}

    sched = DerivationScheduler(
        store=store,
        policy=POLICY,
        run_stage=run_stage,
        stages=lambda: ["s1"],
        maintenance_stages=lambda: [],
        clock=clock,
        idle_timers=False,
    )
    holder["s"] = sched
    await sched.sleep("u").wait(timeout=5)
    assert store.state("u") is None or store.state("u")["deposits_since_run"] == 0  # type: ignore[index]
    assert not in_derive_job()
    await sched.stop()


async def test_open_catches_up_from_persisted_counters(tmp_path: Path) -> None:
    clock = Clock()
    path = str(tmp_path / "l.db")
    first = LeaseStore(path, clock=clock)
    for _ in range(3):
        first.bump_deposits("u")
    ran: list[str] = []

    async def run_stage(stage: str) -> dict[str, object]:
        ran.append(stage)
        return {"status": "ok"}

    clock.advance(3600)  # the process was gone for an hour: idle time counts
    sched = DerivationScheduler(
        store=LeaseStore(path, clock=clock),
        policy=TriggerPolicy(TriggerConfig(min_turns=3, idle=timedelta(minutes=10))),
        run_stage=run_stage,
        stages=lambda: ["s1"],
        maintenance_stages=lambda: [],
        clock=clock,
        idle_timers=False,
    )
    assert await sched.on_open() == ["u"]
    await asyncio.gather(*sched._tasks)
    assert ran == ["s1"]
    await sched.stop()


# ----------------------------------------------------------------------------- daily maintenance


async def test_daily_maintenance_schedule(tmp_path: Path) -> None:
    clock = Clock()
    store = LeaseStore(str(tmp_path / "l.db"), clock=clock)
    ran: list[str] = []

    async def run_stage(stage: str) -> dict[str, object]:
        ran.append(stage)
        return {"status": "ok"}

    cfg = TriggerConfig(
        min_turns=3,
        idle=timedelta(minutes=10),
        min_gap=timedelta(hours=1),
        maintenance_interval=timedelta(hours=24),
    )
    sched = DerivationScheduler(
        store=store,
        policy=TriggerPolicy(cfg),
        run_stage=run_stage,
        stages=lambda: ["consolidate", "decay_sweep", "compress"],
        maintenance_stages=lambda: ["decay_sweep", "compress"],
        clock=clock,
        idle_timers=False,
    )
    store.bump_deposits("u")  # state row exists, below N: no sleep; maintenance not due yet
    clock.advance(60)
    assert not sched.evaluate("u").fire
    await asyncio.sleep(0.05)
    assert ran == [] and not sched._tasks
    clock.advance(23 * 3600)  # still inside the first day
    sched.evaluate("u")
    await asyncio.sleep(0.05)
    assert ran == []
    clock.advance(2 * 3600)  # past 24 h since the first deposit: only the maintenance stages
    d = sched.evaluate("u")
    assert not d.fire
    await asyncio.gather(*sched._tasks)
    assert ran == ["decay_sweep", "compress"]
    ran.clear()
    clock.advance(25 * 3600)  # the next day
    sched.evaluate("u")
    await asyncio.gather(*sched._tasks)
    assert ran == ["decay_sweep", "compress"]
    ran.clear()
    # a full sleep counts as the day's maintenance
    for _ in range(3):
        store.bump_deposits("u")
    clock.advance(11 * 60)
    assert sched.evaluate("u").fire
    await asyncio.gather(*sched._tasks)
    assert ran == ["consolidate", "decay_sweep", "compress"]
    ran.clear()
    clock.advance(23 * 3600)
    sched.evaluate("u")
    await asyncio.gather(*sched._tasks)
    assert ran == []
    await sched.stop()


async def test_maintenance_stage_order_is_subset_of_sleep_order(tmp_path: Path) -> None:
    eng = _engine(tmp_path, sleep_v2=True, sleep_manual_only=True)
    await eng.start()
    try:
        from memspine.workers.schedule import maintenance_stage_order, sleep_stage_order

        ctx = eng._pipeline_ctx()
        assert maintenance_stage_order(ctx) == ("decay_sweep", "compress")
        assert tuple(sleep_stage_order(ctx)) == SLEEP_CYCLE_ORDER
    finally:
        await eng.stop()


# ----------------------------------------------------------------------------- priority


@pytest.mark.parametrize(
    ("background", "expected"), [(True, Priority.DERIVE), (False, Priority.READ)]
)
async def test_background_derive_priority(
    tmp_path: Path, background: bool, expected: Priority
) -> None:
    seen: list[Priority] = []

    async def run_stage(stage: str) -> dict[str, object]:
        seen.append(current_priority())
        return {"status": "ok"}

    sched = DerivationScheduler(
        store=LeaseStore(":memory:"),
        policy=POLICY,
        run_stage=run_stage,
        stages=lambda: ["s1"],
        maintenance_stages=lambda: [],
        idle_timers=False,
        background_priority=background,
    )
    await sched.sleep("u").wait(timeout=5)
    assert seen == [expected]
    await sched.stop()
