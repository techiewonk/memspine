"""RM-132 (ADR-091): the Temporal Activity drains the outbox through the inline fenced path.

The Activity body (``OutboxActivity.drain``) is a plain async function, so every directive §8
scenario runs here without ``temporalio``: a crash between run and commit, duplicate delivery of
the same offset, erasure during the Activity, and a runner switch mid-queue. Two runners on one
SQLite file stand in for two processes. The temporalio-backed checks at the bottom skip when the
``[temporal]`` extra is absent; live-server gates stay RM-16."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine.clients.sqlite.sync import sqlite_sync_engine
from memspine.exceptions import MissingServiceError
from memspine.observability import tap
from memspine.runtime.jobs import ManualClock, RetryBudget
from memspine.runtime.outbox import EffectFence, SqlJobStore
from memspine.runtime.runner import InlineRunner, StageHandler
from memspine.runtime.temporal_outbox import (
    OUTBOX_DRAIN_ACTIVITY,
    InvalidDrainRequest,
    OutboxActivity,
    OutboxActivityFenced,
    drain_request,
    kick_outbox_drain,
    outbox_workflow_id,
    temporal_failure,
)
from memspine.schemas.durable.common import OwnerScope

SCOPE = OwnerScope(tenant="t", owner="ns")
STAGE = "derive"
T0 = datetime(2026, 11, 1, tzinfo=UTC)
LEASE = timedelta(minutes=5)


def _has_temporalio() -> bool:
    try:
        import temporalio  # noqa: F401
    except ImportError:
        return False
    return True


@pytest.fixture(autouse=True)
def _tap_off():
    tap.shutdown()
    yield
    tap.shutdown()


class ProcessDied(BaseException):
    """A hard crash: not an ``Exception``, so no handler turns it into a hole or a release."""


class FactStore:
    """A provider projection shared by every runner: facts keyed by offset, idempotent deposits."""

    def __init__(self) -> None:
        self.facts: dict[int, list[str]] = {}
        self.commits: Counter[int] = Counter()
        self.compensated: list[int] = []
        self.projected = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False
        self.after_commit: Any = None

    async def prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return list(payload.get("record_ids", []))

    async def commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        self.commits[offset] += 1
        self.facts[offset] = list(prepared)  # idempotent: a replay overwrites, never duplicates
        self.projected.set()
        if self.hold:
            await self.release.wait()
        if self.after_commit is not None:
            self.after_commit(offset)

    async def compensate(
        self, scope: OwnerScope, offset: int, prepared: Any, effects: Sequence[dict[str, Any]]
    ) -> None:
        self.facts.pop(offset, None)
        self.compensated.append(offset)

    def handler(self) -> StageHandler:
        return StageHandler(
            self.prepare,
            self.commit,
            effect_results=lambda prepared: {"facts": len(prepared)},
            compensate=self.compensate,
        )


def _runner(db: Path, clock: ManualClock, facts: FactStore, owner: str) -> InlineRunner:
    runner = InlineRunner(
        SqlJobStore(sqlite_sync_engine(str(db), busy_timeout_s=10.0)),
        clock=clock,
        owner=owner,
        lease_ttl=LEASE,
        budget=RetryBudget(base_backoff=timedelta(0)),
    )
    runner.register(STAGE, facts.handler())
    return runner


async def _seed(runner: InlineRunner, n: int) -> None:
    sources = [
        {"record_id": f"r{i}", "source_version": 1, "content_hash": f"h{i}"} for i in range(n)
    ]
    runner.store.mark_sources(SCOPE, STAGE, sources, T0)
    for i in range(n):
        await runner.enqueue(SCOPE, STAGE, {"record_ids": [f"r{i}"]})


async def _drain_all(activity: OutboxActivity, max_items: int = 2) -> list[dict[str, Any]]:
    steps = []
    for _ in range(50):
        result = await activity.drain(drain_request(SCOPE, STAGE, max_items=max_items))
        steps.append(result)
        if not result["more"]:
            return steps
    raise AssertionError("drain did not converge")


def _complete_effects(runner: InlineRunner) -> list[dict[str, Any]]:
    return [e for e in runner.effect_status(SCOPE) if e.get("state") == "complete"]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "outbox.db"


# --------------------------------------------------------------------------- the step itself
async def test_bounded_steps_walk_the_queue_and_hand_the_job_back(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "temporal-worker")
    await _seed(runner, 3)
    activity = OutboxActivity(runner)
    beats: list[dict[str, Any]] = []

    first = await activity.drain(drain_request(SCOPE, STAGE, max_items=2), heartbeat=beats.append)
    assert first["status"] == "partial" and first["more"] and first["items"] == 2
    assert first["watermark"] == 1
    assert [b["offset"] for b in beats] == [0, 1]
    [job] = runner.book.live_jobs(SCOPE, STAGE)
    assert job.state == "queued" and job.attempts == 0  # handed back, no attempt spent

    second = await activity.drain(drain_request(SCOPE, STAGE, max_items=2))
    assert second["status"] == "drained" and not second["more"] and second["watermark"] == 2
    assert second["job_id"] == first["job_id"]  # the same frozen job resumed at the watermark

    idle = await activity.drain(drain_request(SCOPE, STAGE))
    assert idle["status"] == "idle" and idle["items"] == 0 and not idle["more"]
    assert facts.commits == Counter({0: 1, 1: 1, 2: 1})
    assert len(_complete_effects(runner)) == 3
    assert runner.book.progress(SCOPE, STAGE).holes == []


async def test_a_handler_error_is_a_hole_exactly_as_inline(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")

    async def boom(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        raise RuntimeError("model failed")

    runner.register("broken", StageHandler(boom, facts.commit))
    await runner.enqueue(SCOPE, "broken", {})
    result = await OutboxActivity(runner).drain(drain_request(SCOPE, "broken"))
    assert result["status"] == "drained" and result["holes"] == 1
    [hole] = runner.book.progress(SCOPE, "broken").holes
    assert hole.offset == 0


# --------------------------------------------------------------------------- §8: crash
async def test_crash_between_run_and_commit_retries_and_lands_once(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    first = _runner(db, clock, facts, "attempt-1")
    await _seed(first, 1)
    real_finalize = first.store.finalize_effects

    def die(**kw: Any) -> EffectFence:
        raise ProcessDied  # the provider deposit landed; the process dies before finalize

    first.store.finalize_effects = die  # type: ignore[method-assign]
    with pytest.raises(ProcessDied):
        await OutboxActivity(first).drain(drain_request(SCOPE, STAGE))
    first.store.finalize_effects = real_finalize  # type: ignore[method-assign]
    assert facts.facts == {0: ["r0"]} and facts.commits[0] == 1
    [job] = first.book.live_jobs(SCOPE, STAGE)
    assert job.state == "running"  # the dead attempt's lease is still held

    # Temporal retries the Activity on another worker process (a new store on the same file).
    retry = OutboxActivity(_runner(db, clock, facts, "attempt-2"))
    with pytest.raises(OutboxActivityFenced) as busy:
        await retry.drain(drain_request(SCOPE, STAGE))
    assert busy.value.reason == "busy" and busy.value.retryable
    assert busy.value.retry_after_s == pytest.approx(LEASE.total_seconds())
    assert temporal_failure(busy.value).next_retry_delay_s == pytest.approx(300.0)

    clock.advance(seconds=LEASE.total_seconds() + 1)  # Temporal's retry delay elapses
    done = await retry.drain(drain_request(SCOPE, STAGE))
    assert done["status"] == "drained" and done["watermark"] == 0 and done["holes"] == 0
    assert facts.facts == {0: ["r0"]}  # replayed into the idempotent deposit: one fact
    assert len(_complete_effects(retry.runner)) == 1
    assert retry.runner.book.progress(SCOPE, STAGE).holes == []


async def test_crash_after_finalize_before_checkpoint_skips_the_projection(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    first = _runner(db, clock, facts, "attempt-1")
    await _seed(first, 1)
    real_checkpoint = first.book.checkpoint

    def die(*a: Any, **kw: Any) -> Any:
        raise ProcessDied

    first.book.checkpoint = die  # type: ignore[method-assign]
    with pytest.raises(ProcessDied):
        await OutboxActivity(first).drain(drain_request(SCOPE, STAGE))
    first.book.checkpoint = real_checkpoint  # type: ignore[method-assign]

    clock.advance(seconds=LEASE.total_seconds() + 1)
    retry = OutboxActivity(_runner(db, clock, facts, "attempt-2"))
    done = await retry.drain(drain_request(SCOPE, STAGE))
    assert done["status"] == "drained" and done["watermark"] == 0
    assert facts.commits[0] == 1  # the effect door answered COMPLETE: no second projection
    assert len(_complete_effects(retry.runner)) == 1


# --------------------------------------------------------------------------- §8: duplicates
async def test_duplicate_delivery_of_the_same_offset_runs_once(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")
    await _seed(runner, 1)
    activity = OutboxActivity(runner)
    facts.hold = True

    first = asyncio.create_task(activity.drain(drain_request(SCOPE, STAGE)))
    await asyncio.wait_for(facts.projected.wait(), 5)
    # Temporal delivers the same step again while the first attempt is mid-commit.
    with pytest.raises(OutboxActivityFenced) as dup:
        await activity.drain(drain_request(SCOPE, STAGE))
    assert dup.value.reason == "busy" and not temporal_failure(dup.value).non_retryable
    facts.release.set()
    done = await asyncio.wait_for(first, 5)
    assert done["status"] == "drained"

    again = await activity.drain(drain_request(SCOPE, STAGE))  # a late redelivery
    assert again["status"] == "idle"
    assert facts.commits == Counter({0: 1})
    assert len(_complete_effects(runner)) == 1
    assert runner.book.progress(SCOPE, STAGE).watermark == 0


async def test_duplicate_steps_from_two_workers_never_double_commit(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    a = OutboxActivity(_runner(db, clock, facts, "worker-a"))
    b = OutboxActivity(_runner(db, clock, facts, "worker-b"))
    await _seed(a.runner, 4)

    async def run(activity: OutboxActivity) -> None:
        for _ in range(20):
            try:
                result = await activity.drain(drain_request(SCOPE, STAGE, max_items=1))
            except OutboxActivityFenced as fence:
                assert fence.reason in ("busy", "lease")
                await asyncio.sleep(0)
                continue
            if not result["more"]:
                return

    await asyncio.wait_for(asyncio.gather(run(a), run(b)), 10)
    assert facts.commits == Counter({0: 1, 1: 1, 2: 1, 3: 1})
    assert a.runner.book.progress(SCOPE, STAGE).watermark == 3


# --------------------------------------------------------------------------- §8: erasure
async def test_erasure_during_the_activity_fences_without_a_hole(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")
    await _seed(runner, 2)
    activity = OutboxActivity(runner)
    facts.hold = True

    step = asyncio.create_task(activity.drain(drain_request(SCOPE, STAGE)))
    await asyncio.wait_for(facts.projected.wait(), 5)
    assert facts.facts == {0: ["r0"]}
    assert await runner.erase_source(SCOPE, STAGE, "r0")  # forget lands mid-Activity
    facts.release.set()
    with pytest.raises(OutboxActivityFenced) as fenced:
        await asyncio.wait_for(step, 5)

    assert fenced.value.reason == "source" and fenced.value.retryable
    failure = temporal_failure(fenced.value)
    assert failure.type == "OutboxFenced.source" and not failure.non_retryable
    assert facts.facts == {} and facts.compensated == [0]  # no erased fact survives
    progress = runner.book.progress(SCOPE, STAGE)
    assert progress.watermark == -1 and progress.holes == []  # fenced, not a hole
    [job] = runner.book.live_jobs(SCOPE, STAGE)
    assert job.state == "queued"  # handed back for the retry
    assert runner.effect_status(SCOPE) == []

    # Forget rewrites the queued item; Temporal's retry resumes at the fenced offset.
    facts.hold = False
    await runner.rewrite_pending(
        SCOPE, STAGE, lambda p: {**p, "record_ids": [r for r in p["record_ids"] if r != "r0"]}
    )
    done = await activity.drain(drain_request(SCOPE, STAGE))
    assert done["status"] == "drained" and done["watermark"] == 1 and done["holes"] == 0
    assert all("r0" not in recs for recs in facts.facts.values())
    assert facts.facts[1] == ["r1"]


async def test_a_diverged_replay_is_non_retryable_and_keeps_the_job(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")
    await _seed(runner, 1)
    runner.store.authorize_effects = lambda **kw: EffectFence.REPLAY_DIVERGED  # type: ignore[method-assign]
    with pytest.raises(OutboxActivityFenced) as fenced:
        await OutboxActivity(runner).drain(drain_request(SCOPE, STAGE))
    assert fenced.value.reason == "replay" and not fenced.value.retryable
    failure = temporal_failure(fenced.value)
    assert failure.non_retryable and failure.next_retry_delay_s is None
    progress = runner.book.progress(SCOPE, STAGE)
    assert progress.watermark == -1 and progress.holes == []
    [job] = runner.book.live_jobs(SCOPE, STAGE)
    assert job.state == "queued" and job.attempts == 0
    assert facts.commits == Counter()


async def test_a_lost_lease_is_retryable(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")
    await _seed(runner, 2)

    def steal(offset: int) -> None:
        [job] = runner.book.live_jobs(SCOPE, STAGE)
        runner.book.cancel(job.job_id)  # bumps the epoch: this attempt is fenced

    facts.after_commit = steal
    with pytest.raises(OutboxActivityFenced) as fenced:
        await OutboxActivity(runner).drain(drain_request(SCOPE, STAGE))
    assert fenced.value.reason == "lease" and fenced.value.retryable
    assert runner.book.progress(SCOPE, STAGE).holes == []


async def test_a_job_in_retry_backoff_is_busy_until_its_not_before(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")
    runner.book.budget = RetryBudget(base_backoff=timedelta(seconds=30))
    await _seed(runner, 1)
    claim = runner.book.claim("other", LEASE)
    assert claim is not None
    assert runner.book.fail(claim.job.job_id, claim.lease.epoch, "transient").ok
    with pytest.raises(OutboxActivityFenced) as busy:
        await OutboxActivity(runner).drain(drain_request(SCOPE, STAGE))
    assert busy.value.reason == "busy" and busy.value.retry_after_s == pytest.approx(30.0)
    clock.advance(seconds=31)
    done = await OutboxActivity(runner).drain(drain_request(SCOPE, STAGE))
    assert done["status"] == "drained" and facts.commits == Counter({0: 1})


# --------------------------------------------------------------------------- §8: runner switch
async def test_runner_switch_mid_queue_inline_then_activity(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    inline = _runner(db, clock, facts, "inline")
    await _seed(inline, 6)

    def stop_after_half(offset: int) -> None:
        if offset == 2:
            inline._stopping = True  # the inline host shuts down mid-job (ER-10 bounded drain)

    facts.after_commit = stop_after_half
    report = await inline.drain()
    facts.after_commit = None
    assert report.stopped and report.released == 1 and report.items == 3
    assert inline.book.progress(SCOPE, STAGE).watermark == 2

    # Temporal takes over in another process, on the same outbox.
    activity = OutboxActivity(_runner(db, clock, facts, "temporal"))
    steps = await _drain_all(activity, max_items=2)
    assert [s["status"] for s in steps] == ["partial", "drained"]
    progress = activity.runner.book.progress(SCOPE, STAGE)
    assert progress.watermark == 5 and progress.holes == []
    assert facts.commits == Counter(dict.fromkeys(range(6), 1))  # no duplicates, no gaps
    assert sorted(facts.facts) == list(range(6))
    assert len(_complete_effects(activity.runner)) == 6


async def test_runner_switch_mid_queue_activity_then_inline(db: Path) -> None:
    clock, facts = ManualClock(T0), FactStore()
    activity = OutboxActivity(_runner(db, clock, facts, "temporal"))
    await _seed(activity.runner, 5)
    partial = await activity.drain(drain_request(SCOPE, STAGE, max_items=2))
    assert partial["status"] == "partial" and partial["watermark"] == 1

    inline = _runner(db, clock, facts, "inline")  # Temporal is switched off; inline resumes
    await inline.enqueue(SCOPE, STAGE, {"record_ids": []})  # new work keeps arriving
    report = await inline.drain()
    assert report.holes == 0 and report.fenced == 0
    assert inline.book.progress(SCOPE, STAGE).watermark == 5
    assert facts.commits == Counter(dict.fromkeys(range(6), 1))


# --------------------------------------------------------------------------- input and seams
@pytest.mark.parametrize(
    "request_",
    [
        None,
        {"tenant": "t", "owner": "ns"},
        {"tenant": "t", "owner": "ns", "stage": " "},
        {"tenant": "t", "owner": "ns", "stage": STAGE, "max_items": 0},
        {"tenant": "t", "owner": "ns", "stage": STAGE, "max_items": True},
        {"tenant": "", "owner": "ns", "stage": STAGE},
        {"tenant": "t", "owner": "ns", "stage": "unregistered"},
    ],
)
async def test_invalid_requests_are_refused(db: Path, request_: Any) -> None:
    runner = _runner(db, ManualClock(T0), FactStore(), "w")
    with pytest.raises(InvalidDrainRequest):
        await OutboxActivity(runner).drain(request_)


def test_fence_mapping_table() -> None:
    rows = {
        reason: temporal_failure(OutboxActivityFenced(reason))
        for reason in ("lease", "busy", "source", "replay")
    }
    assert {r: f.non_retryable for r, f in rows.items()} == {
        "lease": False,
        "busy": False,
        "source": False,
        "replay": True,
    }
    assert rows["source"].next_retry_delay_s == 1.0
    assert rows["replay"].next_retry_delay_s is None
    assert rows["busy"].type == "OutboxFenced.busy"


def test_workflow_id_is_stable_bounded_and_opaque() -> None:
    one = outbox_workflow_id("dep", SCOPE, STAGE)
    assert one == outbox_workflow_id("dep", SCOPE, STAGE)
    assert one != outbox_workflow_id("dep", SCOPE, "other")
    assert one != outbox_workflow_id("dep2", SCOPE, STAGE)
    digest = one.removeprefix("memspine-outbox-")
    assert len(digest) == 32 and set(digest) <= set("0123456789abcdef")  # no scope text


def test_attach_registers_the_activity_on_the_temporal_runner(tmp_path: Path, db: Path) -> None:
    from memspine.workers.temporal_runner import TemporalRunner

    temporal = TemporalRunner(
        address="localhost:7233",
        namespace="default",
        task_queue="q",
        deployment_id="dep",
        journal_path=tmp_path / "journal.db",
        context_factory=lambda: None,  # type: ignore[arg-type,return-value]
    )
    activity = OutboxActivity(_runner(db, ManualClock(T0), FactStore(), "w"))
    activity.attach(temporal)
    assert list(temporal._activities) == [OUTBOX_DRAIN_ACTIVITY]
    with pytest.raises(Exception, match="unique"):
        activity.attach(temporal)


@pytest.mark.skipif(_has_temporalio(), reason="checks the missing-extra error")
async def test_without_temporalio_the_adapters_name_the_extra(db: Path) -> None:
    activity = OutboxActivity(_runner(db, ManualClock(T0), FactStore(), "w"))
    with pytest.raises(MissingServiceError, match=r"memspine\[temporal\]"):
        await activity.temporal_activity(drain_request(SCOPE, STAGE))
    with pytest.raises(MissingServiceError, match=r"memspine\[temporal\]"):
        await kick_outbox_drain(
            object(), deployment_id="dep", task_queue="q", scope=SCOPE, stage=STAGE
        )


# --------------------------------------------------------------------------- temporalio-backed
async def test_temporal_activity_maps_fences_to_application_errors(db: Path) -> None:
    testing = pytest.importorskip("temporalio.testing", reason="requires [temporal]")
    from temporalio.exceptions import ApplicationError

    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")
    await _seed(runner, 2)
    activity = OutboxActivity(runner)
    env = testing.ActivityEnvironment()
    heartbeats: list[Any] = []
    env.on_heartbeat = lambda *details: heartbeats.append(details)

    result = await env.run(activity.temporal_activity, drain_request(SCOPE, STAGE))
    assert result["status"] == "drained" and result["watermark"] == 1
    assert len(heartbeats) == 2

    runner.store.authorize_effects = lambda **kw: EffectFence.REPLAY_DIVERGED  # type: ignore[method-assign]
    await runner.enqueue(SCOPE, STAGE, {"record_ids": ["r0"]})
    with pytest.raises(ApplicationError) as err:
        await env.run(activity.temporal_activity, drain_request(SCOPE, STAGE))
    assert err.value.type == "OutboxFenced.replay" and err.value.non_retryable

    with pytest.raises(ApplicationError) as bad:
        await env.run(activity.temporal_activity, {"stage": STAGE})
    assert bad.value.type == "InvalidOutboxDrain" and bad.value.non_retryable


async def test_drain_workflow_runs_steps_until_done(db: Path) -> None:
    testing = pytest.importorskip("temporalio.testing", reason="requires [temporal]")
    from temporalio import activity as temporal_activity
    from temporalio.worker import Worker

    from memspine.workers.temporal_workflow import OutboxDrainWorkflow

    clock, facts = ManualClock(T0), FactStore()
    runner = _runner(db, clock, facts, "w")
    await _seed(runner, 5)
    outbox = OutboxActivity(runner)

    async def step(request: dict[str, Any]) -> dict[str, Any]:
        return await outbox.temporal_activity(request)

    try:
        env = await testing.WorkflowEnvironment.start_time_skipping()
    except Exception as exc:  # the test server binary may be unavailable offline
        pytest.skip(f"time-skipping test server unavailable: {type(exc).__name__}")
    async with env:
        async with Worker(
            env.client,
            task_queue="outbox-test",
            workflows=[OutboxDrainWorkflow],
            activities=[temporal_activity.defn(name=OUTBOX_DRAIN_ACTIVITY)(step)],
        ):
            handle = await kick_outbox_drain(
                env.client,
                deployment_id="dep",
                task_queue="outbox-test",
                scope=SCOPE,
                stage=STAGE,
                max_items=2,
            )
            totals = await handle.result()
    assert totals["items"] == 5 and totals["steps"] == 3 and totals["watermark"] == 4
    assert facts.commits == Counter(dict.fromkeys(range(5), 1))
