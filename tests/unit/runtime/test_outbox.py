"""F-04: the durable outbox store and the inline runner, with stub handlers (no model, no engine).

Crash/restart, fenced commits, holes and their retry budget, coalescing, the read barrier, the
"no lock across a model call" rule, and the tap events."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine.clients.sqlite.sync import memory_sync_engine, sqlite_sync_engine
from memspine.config.schema.observability import TapConfig
from memspine.observability import tap
from memspine.runtime.jobs import ManualClock, RetryBudget
from memspine.runtime.outbox import OUTBOX_TABLES, EffectFence, SqlJobStore
from memspine.runtime.runner import InlineRunner, StageHandler
from memspine.schemas.durable.common import OwnerScope

SCOPE = OwnerScope(tenant="t", owner="ns")
OTHER = OwnerScope(tenant="t", owner="other")
T0 = datetime(2026, 10, 13, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _tap_off():
    tap.shutdown()
    yield
    tap.shutdown()


class Recorder:
    """A stage handler that records what ran and can fail or block on demand."""

    def __init__(self) -> None:
        self.prepared: list[int] = []
        self.committed: list[int] = []
        self.fail: dict[int, int] = {}  # offset -> failures left (-1: always)
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        self.prepared.append(offset)
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        left = self.fail.get(offset, 0)
        if left != 0:
            self.fail[offset] = left - 1 if left > 0 else left
            raise RuntimeError(f"boom {offset}")
        return payload

    async def commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        self.committed.append(offset)

    def handler(self, **kw: Any) -> StageHandler:
        return StageHandler(self.prepare, self.commit, **kw)


def make_runner(
    engine: Any = None, clock: Any = None, rec: Recorder | None = None, **kw: Any
) -> tuple[InlineRunner, Recorder]:
    rec = rec or Recorder()
    runner = InlineRunner(
        SqlJobStore(engine or memory_sync_engine()),
        clock=clock or ManualClock(T0),
        budget=kw.pop("budget", RetryBudget(base_backoff=timedelta(0))),
        **kw,
    )
    runner.register("derive", rec.handler())
    return runner, rec


def file_engine(tmp_path: Path) -> Any:
    return sqlite_sync_engine(str(tmp_path / "outbox.db"), busy_timeout_s=5.0)


# --------------------------------------------------------------------------- persistence
async def test_tables_exist_and_drain_runs_items_in_order(tmp_path: Path) -> None:
    runner, rec = make_runner(file_engine(tmp_path))
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": i})
    report = await runner.drain()
    assert rec.committed == [0, 1, 2]
    assert report.jobs == 1 and report.items == 3
    con = sqlite3.connect(tmp_path / "outbox.db")
    names = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
    assert set(OUTBOX_TABLES) <= names
    con.close()


async def test_effect_status_is_idempotent_and_fenced_by_current_lease() -> None:
    runner, _ = make_runner()
    await runner.enqueue(SCOPE, "derive", {"n": 0})
    claim = runner.book.claim("worker-a", timedelta(minutes=1))
    assert claim is not None
    identity = {"effect_id": "0123456789abcdef", "sources": ["source-1"]}

    effect = {
        "effect_id": "0123456789abcdef",
        "layer": "facts",
        "identity": identity,
        "count": 2,
        "artifact": None,
    }
    assert (
        runner.store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[],
            at=T0,
        )
        == "authorized"
    )
    assert runner.store.finalize_effects(
        job_id=claim.job.job_id,
        epoch=claim.lease.epoch,
        effects=[effect],
        failure=None,
        at=T0,
    )
    # Replay reuses the effect identity rather than adding another count.
    replay = {**effect, "count": 99}
    assert (
        runner.store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[replay],
            sources=[],
            at=T0,
        )
        == "complete"
    )
    assert runner.store.effect_status(SCOPE) == [
        {
            "effect_id": "0123456789abcdef",
            "job_id": claim.job.job_id,
            "epoch": claim.lease.epoch,
            "stage": "derive",
            "offset": 0,
            "layer": "facts",
            "state": "complete",
            "count": 2,
            "failure": None,
            "artifact": None,
            "identity": identity,
            "committed_at": T0.isoformat(),
        }
    ]
    assert (
        runner.store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch + 1,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[{"effect_id": "fedcba9876543210", "layer": "facts", "identity": {}}],
            sources=[],
            at=T0,
        )
        == EffectFence.STALE_LEASE
    )
    assert len(runner.store.effect_status(SCOPE)) == 1


async def test_effect_status_persists_bounded_failure_metadata() -> None:
    runner, _ = make_runner()
    await runner.enqueue(SCOPE, "derive", {"n": 0})
    claim = runner.book.claim("worker-a", timedelta(minutes=1))
    assert claim is not None
    failure = {
        "stage": "facts",
        "error_class": "malformed",
        "attempts": 2,
        "retry_eligible": False,
        "source_ids": ["source-1"],
    }
    effect = {
        "effect_id": "failure000000001",
        "layer": "facts",
        "identity": {"effect_id": "failure000000001"},
        "count": 0,
        "artifact": None,
    }
    assert (
        runner.store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[],
            at=T0,
        )
        == "authorized"
    )
    assert runner.store.finalize_effects(
        job_id=claim.job.job_id,
        epoch=claim.lease.epoch,
        effects=[effect],
        failure=failure,
        at=T0,
    )
    row = runner.store.effect_status(SCOPE)[0]
    assert row["state"] == "failed" and row["count"] == 0
    assert row["failure"] == failure


async def test_runner_retries_holes_under_fresh_lease_and_replaces_failure() -> None:
    rec = Recorder()
    rec.fail[0] = 1
    runner, _ = make_runner(rec=rec)
    runner.register(
        "derive",
        StageHandler(rec.prepare, rec.commit, effect_results=lambda prepared: {"facts": 0}),
    )
    await runner.enqueue(SCOPE, "derive", {"n": 0})
    first = await runner.drain()
    assert first.holes == 1
    failed = runner.store.effect_status(SCOPE)
    assert len(failed) == 1 and failed[0]["state"] == "failed"

    second = await runner.drain()
    assert second.hole_filled == 1
    completed = runner.store.effect_status(SCOPE)
    assert len(completed) == 1
    assert completed[0]["state"] == "complete" and completed[0]["count"] == 0
    assert completed[0]["failure"] is None
    retry_job = runner.store.get_job(completed[0]["job_id"])
    assert retry_job is not None and retry_job.stage == "derive.hole.0"
    assert retry_job.state == "succeeded" and retry_job.epoch >= 1


async def test_partial_projection_replays_idempotently_from_authorized_event() -> None:
    runner, rec = make_runner()
    projected: set[int] = set()
    calls = 0

    async def partial(scope: OwnerScope, offset: int, prepared: Any) -> None:
        nonlocal calls
        [event] = runner.effect_status(scope)
        assert event["state"] == "authorized"  # event is the door, never an afterthought
        projected.add(offset)  # provider idempotency key consumes the same semantic offset
        calls += 1
        if calls == 1:
            raise RuntimeError("provider stopped after its idempotent write")

    runner.register(
        "derive",
        StageHandler(rec.prepare, partial, effect_results=lambda prepared: {"facts": 1}),
    )
    await runner.enqueue(SCOPE, "derive", {"n": 0})
    first = await runner.drain()
    assert first.holes == 1 and projected == {0}
    [failed] = runner.effect_status(SCOPE)
    assert failed["state"] == "failed"

    second = await runner.drain()
    assert second.hole_filled == 1 and calls == 2 and projected == {0}
    [complete] = runner.effect_status(SCOPE)
    assert complete["state"] == "complete" and complete["count"] == 1


async def test_queued_jobs_survive_a_restart(tmp_path: Path) -> None:
    first, _ = make_runner(file_engine(tmp_path))
    await first.enqueue(SCOPE, "derive", {"n": 0})
    await first.enqueue(SCOPE, "derive", {"n": 1})
    await first.enqueue(OTHER, "derive", {"n": 0})
    first.close()  # process gone: nothing was drained

    second, rec = make_runner(file_engine(tmp_path))
    assert second.depth() == 2  # one job per scope, both still queued
    report = await second.start()  # leftovers drain at start
    assert sorted(rec.committed) == [0, 0, 1]
    assert report.jobs == 2 and second.depth() == 0


async def test_stop_releases_the_job_and_resumes_at_the_watermark(tmp_path: Path) -> None:
    runner, rec = make_runner(file_engine(tmp_path))
    rec.gate = asyncio.Event()
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": i})
    task = asyncio.create_task(runner.drain())
    await asyncio.wait_for(rec.entered.wait(), 5)  # offset 0 is in its (blocked) model call
    stopped = await runner.stop(deadline_s=0.05)  # bounded drain: the call does not finish
    assert stopped.released == 1
    rec.gate.set()
    await asyncio.wait_for(task, 5)
    [job] = [j for j in runner.store.jobs() if j.state == "queued"]
    assert job.attempts == 0  # a release costs no attempt
    assert runner.book.progress(SCOPE, "derive").watermark == -1  # nothing was checkpointed

    # a fresh process: offset 0 is redone, the rest follow, nothing is lost
    again, rec2 = make_runner(file_engine(tmp_path))
    await again.start()
    assert rec2.committed == [0, 1, 2] and again.depth() == 0


async def test_stop_after_a_checkpoint_does_not_repeat_finished_offsets(tmp_path: Path) -> None:
    runner, rec = make_runner(file_engine(tmp_path))
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": i})
    orig = rec.commit

    async def stop_after_first(scope: OwnerScope, offset: int, prepared: Any) -> None:
        await orig(scope, offset, prepared)
        runner._stopping = True  # the stop request lands after offset 0 committed

    runner._handlers["derive"] = StageHandler(rec.prepare, stop_after_first)
    report = await runner.drain()
    assert rec.committed == [0] and report.stopped and report.released == 1
    assert runner.book.progress(SCOPE, "derive").watermark == 0

    again, rec2 = make_runner(file_engine(tmp_path))
    await again.start()
    assert rec2.committed == [1, 2]


# --------------------------------------------------------------------------- fencing
async def test_a_stale_epoch_commit_is_refused_and_logged(tmp_path: Path) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    clock = ManualClock(T0)
    runner, rec = make_runner(file_engine(tmp_path), clock=clock)
    await runner.enqueue(SCOPE, "derive", {})
    ttl = timedelta(seconds=30)
    stale = runner.book.claim("worker-a", ttl)
    assert stale is not None and stale.lease.epoch == 1
    clock.advance(60)  # worker-a stalls past its lease
    fresh = runner.book.claim("worker-b", ttl)
    assert fresh is not None and fresh.lease.epoch == 2

    refused = runner.book.commit(stale.job.job_id, 1)
    assert not refused.ok and refused.reason is not None
    assert not runner.book.checkpoint(stale.job.job_id, 1, 0).ok
    assert runner.book.progress(SCOPE, "derive").watermark == -1  # the stale worker wrote nothing
    assert runner.book.commit(fresh.job.job_id, 2).ok

    log = runner.store.log()
    assert {e.op for e in log} == {"commit", "checkpoint"}
    assert all(e.presented_epoch == 1 and e.current_epoch == 2 for e in log)
    stages = [e["stage"] for t in tap.memory_traces() for e in t["events"]]
    assert "runtime.outbox.fence_refused" in stages
    assert rec.committed == []  # the library calls above never ran a handler


async def test_effect_door_refuses_a_stale_worker_before_projection() -> None:
    clock = ManualClock(T0)
    rec = Recorder()
    rec.gate = asyncio.Event()
    runner, _ = make_runner(clock=clock, rec=rec)
    runner.register(
        "derive",
        StageHandler(rec.prepare, rec.commit, effect_results=lambda prepared: {"facts": 1}),
    )
    source = {"record_id": "r1", "source_version": 1, "content_hash": "hash-1"}
    runner.store.mark_sources(SCOPE, "derive", [source], T0)
    await runner.enqueue(SCOPE, "derive", {"record_ids": ["r1"]})
    task = asyncio.create_task(runner.drain())
    await asyncio.wait_for(rec.entered.wait(), 5)
    clock.advance(minutes=6)
    fresh = runner.book.claim("worker-b", timedelta(minutes=1))
    assert fresh is not None and fresh.lease.epoch == 2
    rec.gate.set()
    report = await asyncio.wait_for(task, 5)
    assert report.fenced == 1 and rec.committed == []
    assert runner.store.effect_status(SCOPE) == []


async def test_effect_door_refuses_an_expired_unreclaimed_lease() -> None:
    clock = ManualClock(T0)
    runner, _ = make_runner(clock=clock)
    await runner.enqueue(SCOPE, "derive", {})
    claim = runner.book.claim("worker-a", timedelta(seconds=30))
    assert claim is not None
    effect = {
        "effect_id": "expired000000001",
        "layer": "facts",
        "identity": {"effect_id": "expired000000001", "sources": []},
        "count": 1,
        "artifact": None,
    }
    clock.advance(31)
    assert (
        runner.store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[],
            at=clock(),
        )
        == EffectFence.STALE_LEASE
    )

    # Expiry is also checked at acknowledgement when authorization happened
    # while the lease was live but provider work outlasted it.
    fresh = runner.book.claim("worker-b", timedelta(seconds=30))
    assert fresh is not None
    assert (
        runner.store.authorize_effects(
            job_id=fresh.job.job_id,
            epoch=fresh.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[],
            at=clock(),
        )
        == "authorized"
    )
    clock.advance(31)
    assert not runner.store.finalize_effects(
        job_id=fresh.job.job_id,
        epoch=fresh.lease.epoch,
        effects=[effect],
        failure=None,
        at=clock(),
    )
    assert runner.store.effect_status(SCOPE)[0]["state"] == "authorized"


async def test_effect_door_revalidates_sources_after_provider_work() -> None:
    runner, _ = make_runner()
    source = {
        "namespace": SCOPE.owner,
        "record_id": "r1",
        "source_id": "message-1",
        "source_version": 1,
        "content_hash": "hash-1",
        "erasure_generation": 0,
    }
    runner.store.mark_sources(SCOPE, "derive", [source], T0)
    await runner.enqueue(SCOPE, "derive", {"record_ids": ["r1"]})
    claim = runner.book.claim("worker-a", timedelta(minutes=1))
    assert claim is not None
    effect = {
        "effect_id": "revalidate000001",
        "layer": "facts",
        "identity": {"effect_id": "revalidate000001", "sources": [source]},
        "count": 1,
        "artifact": None,
    }
    assert (
        runner.store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[source],
            at=T0,
        )
        == "authorized"
    )
    assert runner.store.erase_source(SCOPE, "derive", "r1")
    assert not runner.store.finalize_effects(
        job_id=claim.job.job_id,
        epoch=claim.lease.epoch,
        effects=[effect],
        failure=None,
        at=T0,
    )
    assert runner.store.effect_status(SCOPE) == []


async def test_effect_door_refuses_forget_during_inference() -> None:
    rec = Recorder()
    rec.gate = asyncio.Event()
    runner, _ = make_runner(rec=rec)
    runner.register(
        "derive",
        StageHandler(rec.prepare, rec.commit, effect_results=lambda prepared: {"facts": 1}),
    )
    source = {"record_id": "r1", "source_version": 1, "content_hash": "hash-1"}
    runner.store.mark_sources(SCOPE, "derive", [source], T0)
    await runner.enqueue(SCOPE, "derive", {"record_ids": ["r1"]})
    task = asyncio.create_task(runner.drain())
    await asyncio.wait_for(rec.entered.wait(), 5)
    assert runner.store.erase_source(SCOPE, "derive", "r1")
    rec.gate.set()
    report = await asyncio.wait_for(task, 5)
    # RM-141 C: a fence is not a hole and the watermark stays before the fenced offset.
    assert report.fenced == 1 and report.holes == 0 and rec.committed == []
    assert runner.book.progress(SCOPE, "derive").watermark == -1
    assert runner.store.effect_status(SCOPE) == []


async def test_effect_door_allows_one_concurrent_projection_consumer() -> None:
    runner, _ = make_runner()
    await runner.enqueue(SCOPE, "derive", {})
    claim = runner.book.claim("worker-a", timedelta(minutes=1))
    assert claim is not None
    kw = {
        "job_id": claim.job.job_id,
        "epoch": claim.lease.epoch,
        "scope": SCOPE,
        "stage": "derive",
        "offset": 0,
        "effects": [{"effect_id": "concurrent0000001", "layer": "facts", "identity": {}}],
        "sources": [],
        "at": T0,
    }
    first, second = await asyncio.gather(
        asyncio.to_thread(runner.store.authorize_effects, **kw),
        asyncio.to_thread(runner.store.authorize_effects, **kw),
    )
    assert sorted((first, second)) == ["authorized", "busy"]
    [event] = runner.store.effect_status(SCOPE)
    assert event["state"] == "authorized"


async def test_a_worker_fenced_mid_job_stops_without_committing(tmp_path: Path) -> None:
    runner, rec = make_runner(file_engine(tmp_path))
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": i})
    orig = rec.commit

    async def cancel_during_first(scope: OwnerScope, offset: int, prepared: Any) -> None:
        await orig(scope, offset, prepared)
        [job] = runner.book.live_jobs(SCOPE, "derive")
        runner.book.cancel(job.job_id)  # bumps the epoch: this worker is now stale

    runner._handlers["derive"] = StageHandler(rec.prepare, cancel_during_first)
    report = await runner.drain()
    assert report.fenced == 1 and rec.committed == [0]  # offset 1 never ran
    assert any(e.reason == "stale epoch" or "cancelled" in e.reason for e in runner.store.log())


# --------------------------------------------------------------------------- holes
async def test_a_failed_item_becomes_a_hole_and_is_retried_within_the_budget() -> None:
    runner, rec = make_runner()
    rec.fail = {1: 1}  # fails once, then works
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": i})
    first = await runner.drain()
    assert first.holes == 1 and rec.committed == [0, 2]
    prog = runner.book.progress(SCOPE, "derive")
    assert prog.watermark == 2 and [h.offset for h in prog.open_holes] == [1]
    assert not prog.complete  # the watermark passed offset 1 visibly, not silently

    second = await runner.drain()  # the next drain retries the hole
    assert second.hole_retries == 1 and second.hole_filled == 1
    assert rec.committed == [0, 2, 1]
    assert runner.book.progress(SCOPE, "derive").complete


async def test_a_hole_that_keeps_failing_stops_after_its_budget() -> None:
    runner, rec = make_runner(budget=RetryBudget(max_hole_attempts=3, base_backoff=timedelta(0)))
    rec.fail = {0: -1}
    await runner.enqueue(SCOPE, "derive", {})
    for _ in range(6):
        await runner.drain()
    [hole] = runner.book.progress(SCOPE, "derive").open_holes
    assert hole.attempts == 3 and hole.last_error is not None  # 1 at record + 2 retries
    assert rec.prepared.count(0) == 3  # the budget capped the attempts
    assert runner.book.holes_to_retry(SCOPE, "derive") == []  # visible, no longer retried


# --------------------------------------------------------------------------- coalescing
async def test_triggers_arriving_while_queued_coalesce_into_one_job() -> None:
    runner, rec = make_runner()
    handles = [await runner.enqueue(SCOPE, "derive", {"n": i}) for i in range(4)]
    assert [h.state for h in handles] == ["queued", "coalesced", "coalesced", "coalesced"]
    assert {h.coalesced_into for h in handles[1:]} == {handles[0].job_id}
    live = runner.book.live_jobs(SCOPE, "derive")
    assert len(live) == 1 and live[0].target_offset == 3  # the target rose with each trigger
    await runner.drain()
    assert rec.committed == [0, 1, 2, 3]  # every item ran exactly once, in order
    assert runner.job(handles[0].job_id).state == "succeeded"


async def test_a_trigger_during_a_running_job_makes_one_follow_up() -> None:
    runner, rec = make_runner()
    rec.gate = asyncio.Event()
    await runner.enqueue(SCOPE, "derive", {"n": 0})
    task = asyncio.create_task(runner.drain())
    await asyncio.wait_for(rec.entered.wait(), 5)
    follow = [await runner.enqueue(SCOPE, "derive", {"n": i}) for i in (1, 2)]
    assert follow[0].state == "queued" and follow[1].state == "coalesced"
    rec.gate.set()
    await asyncio.wait_for(task, 5)
    assert rec.committed == [0, 1, 2]  # the frozen job did 0; the follow-up did 1 and 2


# --------------------------------------------------------------------------- read barrier
class Sleeper:
    """Advances the manual clock instead of sleeping, so a timeout is deterministic."""

    def __init__(self, clock: ManualClock) -> None:
        self.clock = clock
        self.calls = 0

    async def __call__(self, seconds: float) -> None:
        self.calls += 1
        self.clock.advance(1.0)
        await asyncio.sleep(0)


async def test_barrier_ready_when_the_watermark_reaches_the_captured_target() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    clock = ManualClock(T0)
    runner, rec = make_runner(clock=clock, sleeper=Sleeper(clock))
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": i})
    out = await runner.wait(SCOPE, ["derive"], timeout=30)  # drains inline while it waits
    assert out.status == "ready" and out.targets == {"derive": 2} and out.missing == ()
    assert rec.committed == [0, 1, 2]
    stages = [e["stage"] for t in tap.memory_traces() for e in t["events"]]
    assert stages.count("runtime.barrier.wait") == 2  # begin and end


async def test_barrier_returns_partial_after_the_timeout() -> None:
    clock = ManualClock(T0)
    sleeper = Sleeper(clock)
    runner, rec = make_runner(clock=clock, sleeper=sleeper)
    rec.gate = asyncio.Event()  # the model call never returns inside the timeout
    await runner.enqueue(SCOPE, "derive", {})
    drain = asyncio.create_task(runner.drain())
    await asyncio.wait_for(rec.entered.wait(), 5)
    out = await runner.wait(SCOPE, ["derive", "graph"], timeout=5)
    assert out.status == "partial"
    assert out.pending == ("derive",) and out.unavailable == (
        "graph",
    )  # not configured: not waited
    assert out.missing == ("derive", "graph")
    assert sleeper.calls >= 5  # it waited for the full timeout before giving up
    rec.gate.set()
    await asyncio.wait_for(drain, 5)
    assert (await runner.wait(SCOPE, ["derive"], timeout=1)).status == "ready"


async def test_barrier_target_is_captured_when_the_wait_begins() -> None:
    clock = ManualClock(T0)
    runner, _ = make_runner(clock=clock, sleeper=Sleeper(clock))
    await runner.enqueue(SCOPE, "derive", {"n": 0})
    out = await runner.wait(SCOPE, ["derive"], timeout=5)
    await runner.enqueue(
        SCOPE, "derive", {"n": 1}
    )  # a later write does not make the earlier wait stale
    assert out.status == "ready" and out.targets == {"derive": 0}
    again = await runner.wait(SCOPE, ["derive"], timeout=5)
    assert again.targets == {"derive": 1} and again.status == "ready"


async def test_barrier_with_nothing_enqueued_is_ready_and_reports_holes() -> None:
    runner, rec = make_runner()
    assert (await runner.wait(SCOPE, ["derive"], timeout=0)).status == "ready"
    rec.fail = {0: -1}
    await runner.enqueue(SCOPE, "derive", {})
    out = await runner.wait(SCOPE, ["derive"], timeout=5)
    assert out.status == "ready" and out.holes == {"derive": (0,)}  # passed visibly, not blocked


# --------------------------------------------------------------------------- locks
async def test_no_namespace_lock_is_held_during_prepare_but_commit_takes_it() -> None:
    locks: dict[str, asyncio.Lock] = {}

    def lock_for(ns: str) -> asyncio.Lock:
        return locks.setdefault(ns, asyncio.Lock())

    seen: dict[str, list[bool]] = {"prepare": [], "commit": []}

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        seen["prepare"].append(lock_for(scope.owner).locked())  # the (stubbed) model call
        await asyncio.sleep(0)
        return payload

    async def commit(scope: OwnerScope, offset: int, prepared: Any) -> None:
        seen["commit"].append(lock_for(scope.owner).locked())

    runner = InlineRunner(
        SqlJobStore(memory_sync_engine()), clock=ManualClock(T0), commit_lock=lock_for
    )
    runner.register("derive", StageHandler(prepare, commit, commit_locked=True))
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": i})
    await runner.drain()
    assert seen == {"prepare": [False] * 3, "commit": [True] * 3}
    assert not lock_for("ns").locked()


async def test_a_writer_is_not_blocked_by_a_model_call_in_flight() -> None:
    lock = asyncio.Lock()
    release = asyncio.Event()

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        await release.wait()  # a slow model call

    async def commit(scope: OwnerScope, offset: int, prepared: Any) -> None:
        return None

    runner = InlineRunner(
        SqlJobStore(memory_sync_engine()), clock=ManualClock(T0), commit_lock=lambda ns: lock
    )
    runner.register("derive", StageHandler(prepare, commit, commit_locked=True))
    await runner.enqueue(SCOPE, "derive", {})
    task = asyncio.create_task(runner.drain())
    await asyncio.sleep(0.05)
    async with asyncio.timeout(1):  # the 2026-10-12 hang: this would wait forever
        async with lock:
            pass
    release.set()
    await asyncio.wait_for(task, 5)


# --------------------------------------------------------------------------- tap, pending rewrite
async def test_tap_events_cover_the_job_lifecycle() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    runner, rec = make_runner()
    rec.fail = {0: 1}
    await runner.enqueue(SCOPE, "derive", {})
    await runner.drain()
    await runner.drain()
    stages = {e["stage"] for t in tap.memory_traces() for e in t["events"]}
    assert {
        "runtime.outbox.enqueue",
        "runtime.outbox.claim",
        "runtime.outbox.commit",
        "runtime.outbox.hole",
        "runtime.outbox.drain",
    } <= stages


async def test_rewrite_pending_only_touches_unhandled_items() -> None:
    runner, _ = make_runner()
    await runner.enqueue(SCOPE, "derive", {"ids": ["a", "b"]})
    await runner.drain()
    await runner.enqueue(SCOPE, "derive", {"ids": ["a", "c"]})
    n = await runner.rewrite_pending(
        SCOPE, "derive", lambda p: {"ids": [i for i in p["ids"] if i != "a"]}
    )
    assert n == 1
    assert runner.pending_items(SCOPE, "derive") == [(1, {"ids": ["c"]})]
    assert runner.store.get_item(SCOPE, "derive", 0) == {"ids": ["a", "b"]}  # already handled
