"""RT-01: taskiq transport over the outbox ("outbox = truth, taskiq = transport").

Flag ``workers.transport`` (default ``inline``). Crash tests with the in-memory broker and with an
external-style broker (a stub queue shared by a producer and a worker, since Redis is not run here),
plus the Redis broker factory. Every crash test ends on the same claim: each offset commits once."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("taskiq")  # the taskiq extra is not in the dev env (D-41)

from taskiq import InMemoryBroker
from taskiq.abc.broker import AsyncBroker
from taskiq.message import BrokerMessage
from taskiq.receiver import Receiver

from memspine.clients.sqlite.sync import sqlite_sync_engine
from memspine.config.schema import MemspineConfig
from memspine.config.schema.observability import TapConfig
from memspine.observability import tap
from memspine.runtime.container import make_outbox_runner
from memspine.runtime.jobs import ManualClock, RetryBudget
from memspine.runtime.outbox import SqlJobStore
from memspine.runtime.runner import InlineRunner, StageHandler
from memspine.runtime.transport import TASK_NAME, TransportRunner, make_broker
from memspine.schemas.durable.common import OwnerScope

SCOPE = OwnerScope(tenant="t", owner="ns")
T0 = datetime(2026, 10, 13, tzinfo=UTC)
TTL = timedelta(seconds=30)


def _runner_dependencies() -> tuple[Any, Any]:
    """Create the generic SQL capability and resource owner used by the runtime builder."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from memspine.clients.sqlite.sync import memory_sync_engine
    from memspine.providers.lifecycle import ProviderLifecycleManager
    from memspine.providers.sql import ManagedSqlAccess

    async_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    resources = ProviderLifecycleManager(close_timeout_s=2.0)

    async def close_storage() -> None:
        await async_engine.dispose()

    resources.own("storage", close_storage)

    async def checkpoint() -> None:
        return None

    sql = ManagedSqlAccess(
        async_engine,
        is_memory=True,
        capabilities=frozenset({"sqlite", "memory"}),
        checkpoint=checkpoint,
        sync_factory=memory_sync_engine,
        owner_resource_id="storage",
    )
    return sql, resources


def _config(**kwargs: Any) -> MemspineConfig:
    return MemspineConfig(
        runtime={
            "provider_services": {
                "health_timeout_s": 2.0,
                "close_timeout_s": 2.0,
                "startup_timeout_s": None,
            }
        },
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _tap_off():
    tap.shutdown()
    yield
    tap.shutdown()


class Recorder:
    def __init__(self) -> None:
        self.committed: list[int] = []
        self.gate: asyncio.Event | None = None
        self.entered = asyncio.Event()

    async def prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        self.entered.set()
        if self.gate is not None:
            await self.gate.wait()
        return payload

    async def commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        self.committed.append(offset)

    def handler(self) -> StageHandler:
        return StageHandler(self.prepare, self.commit)


class QueueBroker(AsyncBroker):
    """An external-style broker: messages leave the producer and wait in a queue a worker reads."""

    def __init__(self, queue: asyncio.Queue[bytes] | None = None, *, down: bool = False) -> None:
        super().__init__()
        self.queue: asyncio.Queue[bytes] = queue or asyncio.Queue()
        self.down = down

    async def kick(self, message: BrokerMessage) -> None:
        if self.down:
            raise ConnectionError("broker unreachable")
        await self.queue.put(message.message)

    async def listen(self) -> AsyncGenerator[bytes]:
        while True:
            yield await self.queue.get()


def db(tmp_path: Path) -> Any:
    return sqlite_sync_engine(str(tmp_path / "outbox.db"), busy_timeout_s=5.0)


def make(
    engine: Any,
    broker: Any,
    clock: Any,
    rec: Recorder,
    *,
    local_drain: bool = True,
    owner: str = "w",
) -> TransportRunner:
    runner = TransportRunner(
        SqlJobStore(engine),
        broker=broker,
        local_drain=local_drain,
        clock=clock,
        owner=owner,
        lease_ttl=TTL,
        budget=RetryBudget(base_backoff=timedelta(0)),
    )
    runner.register("derive", rec.handler())
    return runner


async def _items(runner: TransportRunner, n: int) -> None:
    for i in range(n):
        await runner.enqueue(SCOPE, "derive", {"i": i})


# ----------------------------------------------------------------------------- the flag
def test_flag_defaults_off_and_inline_builds_the_inline_runner() -> None:
    cfg = _config()
    assert cfg.workers.transport == "inline" and cfg.workers.transport_broker == "memory"
    sql, resources = _runner_dependencies()
    try:
        runner = make_outbox_runner(cfg, sql, resources)
        assert type(runner) is InlineRunner
    finally:
        asyncio.run(resources.close_all())


def test_flag_on_builds_a_transport_runner_over_the_memory_broker() -> None:
    cfg = _config(workers={"outbox": True, "transport": "taskiq"})
    sql, resources = _runner_dependencies()
    try:
        runner = make_outbox_runner(cfg, sql, resources)
        assert isinstance(runner, TransportRunner) and runner.local_drain
        assert isinstance(runner.broker, InMemoryBroker)
    finally:
        asyncio.run(resources.close_all())


def test_redis_broker_is_built_lazily_from_the_url() -> None:
    pytest.importorskip("taskiq_redis")
    cfg = _config(
        workers={
            "outbox": True,
            "transport": "taskiq",
            "transport_broker": "redis",
            "broker_url": "redis://localhost:6399/3",
        }
    )
    sql, resources = _runner_dependencies()
    try:
        runner = make_outbox_runner(cfg, sql, resources)
        assert isinstance(runner, TransportRunner) and not runner.local_drain  # producer only
        assert type(runner.broker).__name__ == "RedisStreamBroker"
        assert make_broker("memory").__class__ is InMemoryBroker
    finally:
        asyncio.run(resources.close_all())


# ----------------------------------------------------------------------------- in-memory broker
async def test_memory_broker_drains_in_process_from_the_message_alone(tmp_path: Path) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    rec = Recorder()
    runner = make(db(tmp_path), InMemoryBroker(), ManualClock(T0), rec)
    await _items(runner, 3)
    await runner.broker.wait_all()  # no explicit drain: the task did the work
    assert sorted(rec.committed) == [0, 1, 2] and len(rec.committed) == 3
    stages = {e["stage"] for t in tap.memory_traces() for e in t["events"]}
    assert {"runtime.transport.kick", "runtime.transport.receive"} <= stages


async def test_a_duplicate_message_is_a_noop(tmp_path: Path) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    rec = Recorder()
    runner = make(db(tmp_path), InMemoryBroker(), ManualClock(T0), rec)
    handle = await runner.enqueue(SCOPE, "derive", {})
    await runner.broker.wait_all()
    await runner.kick(handle.job_id)
    await runner.kick(handle.job_id)
    await runner.broker.wait_all()
    assert rec.committed == [0]
    stages = [e["stage"] for t in tap.memory_traces() for e in t["events"]]
    assert "runtime.transport.skip" in stages


async def test_crash_with_an_abandoned_lease_resends_on_start_and_runs_once(
    tmp_path: Path,
) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    clock = ManualClock(T0)
    dead = make(db(tmp_path), QueueBroker(), clock, Recorder(), local_drain=False, owner="dead")
    await _items(dead, 3)
    claim = dead.book.claim("dead", TTL)  # leased, then the process dies: nothing committed
    assert claim is not None and dead.store.get_job(claim.job.job_id).state == "running"
    dead.store.close()

    rec = Recorder()
    clock.advance(120)  # the lease ran out
    second = make(db(tmp_path), InMemoryBroker(), clock, rec)
    await second.start()
    await second.broker.wait_all()
    assert rec.committed == [0, 1, 2]
    await second.start()  # a second start finds nothing unfinished
    await second.broker.wait_all()
    assert rec.committed == [0, 1, 2]
    assert not [j for j in second.store.jobs() if j.state in ("queued", "running")]


async def test_cancelled_mid_drain_then_restart_commits_each_offset_once(tmp_path: Path) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    clock = ManualClock(T0)
    rec1 = Recorder()
    rec1.gate = asyncio.Event()
    first = make(db(tmp_path), InMemoryBroker(), clock, rec1)
    await _items(first, 3)  # the message-driven drain blocks inside offset 0
    await asyncio.wait_for(rec1.entered.wait(), 5)
    for task in list(first.broker._running_tasks):  # the crash: the drain task is cancelled
        task.cancel()
    await asyncio.gather(*first.broker._running_tasks, return_exceptions=True)
    assert rec1.committed == []
    first.store.close()

    rec2 = Recorder()
    clock.advance(120)
    second = make(db(tmp_path), InMemoryBroker(), clock, rec2)
    await second.start()
    await second.broker.wait_all()
    assert rec2.committed == [0, 1, 2]
    assert second.book.progress(SCOPE, "derive").watermark == 2
    stages = {e["stage"] for t in tap.memory_traces() for e in t["events"]}
    assert "runtime.transport.resend" in stages and "runtime.transport.start" in stages


async def test_lost_messages_are_resent_from_the_outbox_on_start(tmp_path: Path) -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    clock = ManualClock(T0)
    producer = make(db(tmp_path), QueueBroker(down=True), clock, Recorder(), local_drain=False)
    await _items(producer, 3)
    assert producer.kick_failures == 3  # the broker never got them; the rows are durable
    producer.store.close()
    stages = {e["stage"] for t in tap.memory_traces() for e in t["events"]}
    assert "runtime.transport.kick_failed" in stages

    rec = Recorder()
    restarted = make(db(tmp_path), InMemoryBroker(), clock, rec)
    await restarted.start()
    await restarted.broker.wait_all()
    assert rec.committed == [0, 1, 2]


# ------------------------------------------------------------------ external-style broker
async def _listen(worker: TransportRunner) -> tuple[asyncio.Task[Any], asyncio.Event]:
    finish = asyncio.Event()
    receiver = Receiver(worker.broker, run_startup=False, max_async_tasks=4)
    return asyncio.create_task(receiver.listen(finish)), finish


async def _until(pred: Any, seconds: float = 5.0) -> None:
    async with asyncio.timeout(seconds):
        while not pred():  # noqa: ASYNC110 - polling a predicate over plain lists
            await asyncio.sleep(0.01)


async def test_external_broker_producer_enqueues_and_a_worker_drains(tmp_path: Path) -> None:
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    clock = ManualClock(T0)
    producer = make(db(tmp_path), QueueBroker(queue), clock, Recorder(), local_drain=False)
    rec = Recorder()
    worker = make(db(tmp_path), QueueBroker(queue), clock, rec, owner="worker")
    await _items(producer, 3)
    assert (await producer.drain()).items == 0  # the producer leaves draining to the worker
    assert producer.depth() == 1  # three items coalesced into one queued job

    task, finish = await _listen(worker)
    try:
        await _until(lambda: len(rec.committed) == 3)
    finally:
        finish.set()
        await asyncio.wait_for(task, 5)
    assert rec.committed == [0, 1, 2]
    barrier = await producer.wait(SCOPE, ["derive"], 1.0)
    assert barrier.status == "ready"


async def test_external_broker_worker_crash_then_restart_runs_once(tmp_path: Path) -> None:
    queue: asyncio.Queue[bytes] = asyncio.Queue()
    clock = ManualClock(T0)
    producer = make(db(tmp_path), QueueBroker(queue), clock, Recorder(), local_drain=False)
    rec1 = Recorder()
    rec1.gate = asyncio.Event()
    worker1 = make(db(tmp_path), QueueBroker(queue), clock, rec1, owner="w1")
    await _items(producer, 3)

    task, _finish = await _listen(worker1)
    await asyncio.wait_for(rec1.entered.wait(), 5)
    task.cancel()  # the worker process dies inside offset 0, its lease unreleased
    await asyncio.gather(task, return_exceptions=True)
    worker1.store.close()
    assert rec1.committed == []
    assert queue.empty()  # the message was consumed and is gone: only the row remembers

    clock.advance(120)
    rec2 = Recorder()
    worker2 = make(db(tmp_path), QueueBroker(queue), clock, rec2, owner="w2")
    resent = await worker2.resend_unfinished()
    assert resent >= 1
    task2, finish2 = await _listen(worker2)
    try:
        await _until(lambda: len(rec2.committed) == 3)
    finally:
        finish2.set()
        await asyncio.wait_for(task2, 5)
    assert rec2.committed == [0, 1, 2]
    assert worker2.book.progress(SCOPE, "derive").watermark == 2


def test_the_task_name_is_stable() -> None:
    assert TASK_NAME == "memspine.outbox.drain"


# ----------------------------------------------------------------------------- engine level
async def test_engine_with_the_transport_flag_gives_the_same_facts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from test_window_write import FakeWindowLLM, _facts, _send, _turns

    from memspine import Engine
    from memspine.services.llm.base import LLMRouter

    async def run(transport: str) -> tuple[list[str], dict[str, Any]]:
        llm = FakeWindowLLM()

        async def router(
            config: MemspineConfig, hooks: object = None, wiring: object = None
        ) -> LLMRouter:
            return LLMRouter({"window_extract": llm})  # type: ignore[dict-item]

        monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
        eng = Engine(
            template="core",
            dotenv_path=None,
            storage={
                "provider": "sqlite",
                "settings": {"path": str(tmp_path / f"{transport}.db"), "busy_timeout_s": 5.0},
            },
            embedding={"provider": "hash", "settings": {}},
            memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
            write={"window_extract": "facts", "window_async": False, "window_idle_s": 0.0},
            llm={
                "roles": {
                    "window_extract": {
                        "provider": "litellm",
                        "settings": {"model": "fake/window", "timeout_seconds": 20.0},
                    }
                }
            },
            workers={"outbox": True, "transport": transport},
        )
        await eng.start()
        try:
            runner = eng._windows._outbox
            assert isinstance(runner, TransportRunner) == (transport == "taskiq")
            await _send(eng, "a", "s1", _turns("trip", 9))
            stats = await eng.flush_windows()
            return sorted(f.content for f in await _facts(eng, "a")), stats
        finally:
            await eng.stop()

    off_facts, off_stats = await run("inline")
    on_facts, on_stats = await run("taskiq")
    assert off_facts and off_facts == on_facts
    for key in ("windows", "calls", "facts_deposited", "windows_processed"):
        assert off_stats[key] == on_stats[key], key
