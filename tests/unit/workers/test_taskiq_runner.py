"""Taskiq runner (D-16/D-42 §3): per-scope streams, priorities, claim-recovery.

taskiq itself is NOT a dev dependency: the broker mechanics are exercised
against fakeredis (D-41) and the key/label/priority logic as pure functions.
The only taskiq-specific behavior — the import gate — is asserted directly.
"""

from __future__ import annotations

import fakeredis.aioredis
import pytest

from memspine.clients.sqlite import SQLiteClient
from memspine.config.loader import load_config
from memspine.core.events import EventLogMode
from memspine.exceptions import MemspineError, MissingServiceError
from memspine.services.storage.sqlite.engine import SQLiteStorage
from memspine.workers.pipelines import PIPELINES, PipelineContext
from memspine.workers.schedule import SLEEP_CYCLE_ORDER, run_sleep_cycle
from memspine.workers.taskiq_runner import (
    CONSUMER_GROUP,
    DEFAULT_PRIORITY,
    PIPELINE_PRIORITIES,
    TaskiqRunner,
    claim_stale,
    priority_for,
    stream_key,
)


async def make_ctx() -> PipelineContext:
    config = load_config(overrides={"event_log": {"mode": "full"}}).config
    client = SQLiteClient(":memory:")
    await client.connect()
    storage = SQLiteStorage(client, mode=EventLogMode.FULL)
    await storage.start()
    return PipelineContext(storage=storage, config=config)


def make_runner(redis: object) -> TaskiqRunner:
    runner = TaskiqRunner(redis=redis)
    for name, pipeline in PIPELINES.items():
        runner.register(name, pipeline)
    return runner


# ── pure functions (D-42 §3) ──────────────────────────────────────────────────


def test_stream_key_is_per_scope() -> None:
    assert stream_key("org/team-a", "consolidate") == "memspine:org/team-a:consolidate"
    assert stream_key("engine", "check_watches") != stream_key("engine", "compress")
    with pytest.raises(MemspineError):
        stream_key("", "consolidate")
    with pytest.raises(MemspineError):
        stream_key("engine", "")


def test_priority_labels_mirror_the_pipeline_set() -> None:
    """D-42 §3: the label set must never drift from workers.pipelines.PIPELINES."""
    assert set(PIPELINE_PRIORITIES) == set(PIPELINES)
    # Watches beat housekeeping; pruning goes last; priorities are distinct.
    assert PIPELINE_PRIORITIES["check_watches"] < PIPELINE_PRIORITIES["consolidate"]
    assert PIPELINE_PRIORITIES["event_log_prune"] == max(PIPELINE_PRIORITIES.values())
    assert len(set(PIPELINE_PRIORITIES.values())) == len(PIPELINE_PRIORITIES)


def test_unknown_labels_sort_after_everything_shipped() -> None:
    assert priority_for("deployment_custom") == DEFAULT_PRIORITY
    assert priority_for("consolidate") == PIPELINE_PRIORITIES["consolidate"]
    assert max(PIPELINE_PRIORITIES.values()) < DEFAULT_PRIORITY


def test_missing_taskiq_raises_missing_service_naming_the_extra() -> None:
    try:
        import taskiq  # noqa: F401
    except ImportError:
        pass
    else:  # pragma: no cover - env with the extra installed
        pytest.skip("taskiq installed — the import-gate branch is unreachable")
    with pytest.raises(MissingServiceError, match=r"memspine\[taskiq\]"):
        TaskiqRunner()


# ── broker mechanics against fakeredis (D-41) ─────────────────────────────────


async def test_run_executes_pipeline_and_acks_its_marker() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    runner = make_runner(redis)
    ctx = await make_ctx()

    stats = await runner.run("event_log_prune", ctx)
    assert stats["status"] in {"ok", "noop", "skipped"}

    key = stream_key("engine", "event_log_prune")
    assert await redis.xlen(key) == 1  # the durable work marker was appended
    pending = await redis.xpending(key, CONSUMER_GROUP)
    assert pending["pending"] == 0  # ...and acknowledged on success
    await runner.close()


async def test_failed_run_leaves_marker_pending_for_claim_recovery() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    runner = TaskiqRunner(redis=redis)

    async def explode(_ctx: PipelineContext) -> dict[str, object]:
        raise RuntimeError("kaboom")

    runner.register("explode", explode)
    ctx = await make_ctx()
    stats = await runner.run("explode", ctx)
    assert stats["status"] == "error" and "kaboom" in str(stats["error"])

    key = stream_key("engine", "explode")
    pending = await redis.xpending(key, CONSUMER_GROUP)
    assert pending["pending"] == 1  # dead-lettered marker stays claimable

    # XAUTOCLAIM claim-recovery: a second consumer takes over the stale marker.
    claimed = await claim_stale(redis, "engine", "explode", consumer="rescuer", min_idle_ms=0)
    assert len(claimed) == 1
    pending = await redis.xpending(key, CONSUMER_GROUP)
    assert pending["consumers"][0]["name"] == b"rescuer"
    await runner.close()


async def test_unknown_pipeline_errors_without_broker_traffic() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    runner = TaskiqRunner(redis=redis)
    ctx = await make_ctx()
    stats = await runner.run("nope", ctx)
    assert stats["status"] == "error"
    assert await redis.keys() == []  # no stream created for a nonexistent pipeline
    await runner.close()


async def test_broker_outage_degrades_loudly_but_pipelines_still_run() -> None:
    class DownRedis:
        async def xgroup_create(self, *args: object, **kwargs: object) -> None:
            raise ConnectionError("broker down")

    runner = make_runner(DownRedis())
    ctx = await make_ctx()
    stats = await runner.run("event_log_prune", ctx)
    assert stats["status"] in {"ok", "noop", "skipped"}  # maintenance never depends on the broker


async def test_full_sleep_cycle_runs_under_the_taskiq_runner() -> None:
    redis = fakeredis.aioredis.FakeRedis()
    runner = make_runner(redis)
    ctx = await make_ctx()
    results = await run_sleep_cycle(runner, ctx)
    assert set(results) == set(SLEEP_CYCLE_ORDER)
    assert all(stats["status"] in {"ok", "noop", "skipped"} for stats in results.values())
    # One per-scope stream per label — never a shared queue (D-42 §3).
    keys = {key.decode() for key in await redis.keys()}
    assert keys == {stream_key("engine", name) for name in SLEEP_CYCLE_ORDER}
    await runner.close()
