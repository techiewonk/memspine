"""RT-02: priority classes, the global model semaphore, encoder executors, backpressure (coalesce,
never drop) and the SLO ledger. Flags ``runtime.scheduler_v2`` / ``runtime.model_concurrency``,
both OFF by default. Model-free: calls are asyncio sleeps and a hash embedder."""

from __future__ import annotations

import asyncio
import contextlib
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine.clients.sqlite.sync import memory_sync_engine
from memspine.config.schema import MemspineConfig, RuntimeConfig
from memspine.config.schema.observability import TapConfig
from memspine.derive.backpressure import (
    UNDERIVED_KEY,
    Backlog,
    Backpressure,
    BackpressureRejected,
)
from memspine.economy.slo import SloLedger, percentile, timed_read
from memspine.observability import bindings, tap
from memspine.runtime import scheduler as sched
from memspine.runtime.jobs import ManualClock, RetryBudget
from memspine.runtime.outbox import SqlJobStore
from memspine.runtime.runner import InlineRunner
from memspine.runtime.scheduler import EncoderExecutor, ModelGate
from memspine.schemas.durable.common import OwnerScope
from memspine.services.concurrency import (
    Priority,
    current_priority,
    installed_gate,
    model_slot,
    priority_scope,
)

SCOPE = OwnerScope(tenant="t", owner="ns")
T0 = datetime(2026, 10, 13, tzinfo=UTC)


def _runtime(**kwargs: Any) -> RuntimeConfig:
    return RuntimeConfig(
        provider_services={
            "health_timeout_s": 2.0,
            "close_timeout_s": 2.0,
            "startup_timeout_s": None,
        },
        **kwargs,
    )


@pytest.fixture(autouse=True)
def _clean() -> Any:
    tap.shutdown()
    yield
    tap.shutdown()
    assert installed_gate() is None  # no test leaks a gate
    assert not bindings.live()  # every engine unregisters on stop


# ------------------------------------------------------------------------------ flag off
async def test_flags_default_off_and_acquire_is_a_noop() -> None:
    cfg = _runtime()
    assert cfg.scheduler_v2 is False and cfg.model_concurrency == 0
    assert await sched.acquire(cfg) is None


async def test_without_a_gate_model_slot_and_timed_read_are_passthroughs() -> None:
    async with model_slot("llm"):
        pass
    assert current_priority() is Priority.READ

    @timed_read
    async def read() -> int:
        return 7

    assert await read() == 7  # no ledger installed: nothing recorded, nothing raised


async def test_flag_off_runner_runs_items_in_the_callers_priority() -> None:
    seen: list[Priority] = []
    runner = _runner()

    async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        seen.append(current_priority())

    async def commit(scope: OwnerScope, offset: int, prepared: Any) -> None:
        return None

    runner.register("derive", prepare=prepare, commit=commit)
    await runner.enqueue(SCOPE, "derive", {"i": 0})
    await runner.drain()
    assert seen == [Priority.READ]  # untouched: only the flag-on runner sets DERIVE


# ------------------------------------------------------------------------------ the gate
async def test_priority_order_read_then_write_then_derive() -> None:
    gate = ModelGate(1, background_reserve=0)
    order: list[str] = []
    await gate.acquire(Priority.READ)  # the only slot is busy

    async def waiter(name: str, p: Priority) -> None:
        async with gate.slot(p):
            order.append(name)

    tasks = [
        asyncio.create_task(waiter("derive", Priority.DERIVE)),
        asyncio.create_task(waiter("write", Priority.WRITE)),
        asyncio.create_task(waiter("read", Priority.READ)),
        asyncio.create_task(waiter("read2", Priority.READ)),
    ]
    await asyncio.sleep(0.01)
    assert gate.queued == 4
    gate._release(Priority.READ)
    await asyncio.gather(*tasks)
    assert order == ["read", "read2", "write", "derive"]  # class first, FIFO inside a class


async def test_the_semaphore_bounds_concurrency() -> None:
    gate = ModelGate(3, background_reserve=0)
    running = peak = 0

    async def call() -> None:
        nonlocal running, peak
        async with gate.slot(Priority.READ):
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.005)
            running -= 1

    await asyncio.gather(*(call() for _ in range(24)))
    assert peak == 3 and gate.max_active == 3 and gate.active == 0 and gate.queued == 0


async def test_background_reserve_keeps_a_slot_for_the_foreground() -> None:
    gate = ModelGate(3, background_reserve=1)
    assert gate.background_max == 2
    bg = peak_bg = 0

    async def derive() -> None:
        nonlocal bg, peak_bg
        async with gate.slot(Priority.DERIVE):
            bg += 1
            peak_bg = max(peak_bg, bg)
            await asyncio.sleep(0.01)
            bg -= 1

    tasks = [asyncio.create_task(derive()) for _ in range(8)]
    await asyncio.sleep(0.003)
    t0 = time.perf_counter()
    async with gate.slot(Priority.READ):  # a free slot exists although derive is saturated
        assert time.perf_counter() - t0 < 0.005
    await asyncio.gather(*tasks)
    assert peak_bg == 2


async def test_a_cancelled_waiter_leaks_no_slot() -> None:
    gate = ModelGate(1, background_reserve=0)
    await gate.acquire(Priority.READ)
    waiter = asyncio.create_task(gate.acquire(Priority.READ))
    await asyncio.sleep(0.005)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    gate._release(Priority.READ)
    assert gate.active == 0 and gate.queued == 0
    async with gate.slot(Priority.WRITE):  # still usable
        assert gate.active == 1


async def test_scheduler_gate_bounds_the_llm_and_embedding_seams() -> None:
    cfg = _runtime(model_concurrency=2, scheduler_v2=True)
    lease = await sched.acquire(cfg)
    assert lease is not None
    s = lease.value
    try:
        binding = bindings.EngineBinding(gate=s.gate, slo_ledger=s.slo_ledger)
        with bindings.bound(binding):
            assert installed_gate() is s.gate
        running = peak = 0

        async def call() -> None:
            nonlocal running, peak
            async with model_slot("llm"):
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.003)
                running -= 1

        with bindings.bound(binding):
            await asyncio.gather(*(call() for _ in range(10)))
        assert peak == 2
    finally:
        await sched.release(lease)
    assert installed_gate() is None


async def test_gate_tap_events_name_priority_and_wait() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    gate = ModelGate(1, background_reserve=0)
    with tap.request("read"):
        await gate.acquire(Priority.READ)
        t = asyncio.create_task(gate.acquire(Priority.DERIVE))
        await asyncio.sleep(0.005)
        gate._release(Priority.READ)
        await t
        gate._release(Priority.DERIVE)
    names = [e["stage"] for tr in tap.memory_traces() for e in tr["events"]]
    assert names.count("runtime.sched.grant") == 2


# ------------------------------------------------------------------------------ encoder executor
async def test_micro_batching_merges_concurrent_calls_and_keeps_order() -> None:
    calls: list[list[str]] = []

    async def embed(texts: list[str]) -> list[list[float]]:
        calls.append(list(texts))
        await asyncio.sleep(0.01)
        return [[float(len(t))] for t in texts]

    ex = EncoderExecutor("t", embed, max_batch=8, max_wait_s=0.01)
    try:
        with priority_scope(Priority.DERIVE):
            results = await asyncio.gather(*(ex.submit([f"x{'y' * i}"]) for i in range(6)))
        assert [r[0][0] for r in results] == [float(len(f"x{'y' * i}")) for i in range(6)]
        assert ex.batches < 6 and ex.max_batch_seen > 1
        assert sum(len(c) for c in calls) == 6  # nothing dropped, nothing duplicated
    finally:
        await ex.close()


async def test_executor_serves_reads_ahead_of_queued_background_batches() -> None:
    order: list[str] = []
    release = asyncio.Event()

    async def embed(texts: list[str]) -> list[list[float]]:
        order.append(texts[0])
        if texts[0] == "first":
            await release.wait()
        return [[0.0] for _ in texts]

    ex = EncoderExecutor("t", embed, max_batch=1, max_wait_s=0.0)
    try:
        first = asyncio.create_task(ex.submit(["first"], Priority.DERIVE))
        await asyncio.sleep(0.01)
        bg = asyncio.create_task(ex.submit(["bg"], Priority.DERIVE))
        rd = asyncio.create_task(ex.submit(["read"], Priority.READ))
        await asyncio.sleep(0.01)
        release.set()
        await asyncio.gather(first, bg, rd)
        assert order == ["first", "read", "bg"]
    finally:
        await ex.close()


async def test_executor_failure_reaches_every_caller_and_it_keeps_working() -> None:
    boom = {"on": True}

    async def embed(texts: list[str]) -> list[list[float]]:
        if boom["on"]:
            raise RuntimeError("gpu fell over")
        return [[1.0] for _ in texts]

    ex = EncoderExecutor("t", embed, max_batch=8, max_wait_s=0.0)
    try:
        r = await asyncio.gather(ex.submit(["a"]), ex.submit(["b"]), return_exceptions=True)
        assert all(isinstance(x, RuntimeError) for x in r)
        boom["on"] = False
        assert await ex.submit(["c"]) == [[1.0]]
    finally:
        await ex.close()


async def test_batched_embedding_matches_the_hash_embedder() -> None:
    from memspine.services.embedding.hash import HashEmbedding

    inner = (
        HashEmbedding(dim=16)
        if "dim" in HashEmbedding.__init__.__code__.co_varnames
        else HashEmbedding()
    )
    wrapped = sched.Scheduler(_runtime(scheduler_v2=True)).wrap_embedder(inner)
    assert isinstance(wrapped, sched.BatchedEmbedding)
    try:
        texts = [f"memory number {i}" for i in range(9)]
        parts = await asyncio.gather(*(wrapped.embed([t]) for t in texts))
        assert [p[0] for p in parts] == await inner.embed(texts)
        assert wrapped.embedder_id == inner.embedder_id and wrapped.dim == inner.dim
        assert await wrapped.embed_queries(["q"]) == await inner.embed(["q"])
    finally:
        await wrapped.close()


def test_wrap_embedder_is_identity_when_the_flag_is_off() -> None:
    marker = object()
    assert sched.Scheduler(_runtime(model_concurrency=2)).wrap_embedder(marker) is marker


# ------------------------------------------------------------------------------ backpressure policy
def test_policy_accepts_coalesces_then_rejects() -> None:
    bp = Backpressure(coalesce_items=3, max_items=5, coalesce_age_s=10, max_age_s=100)
    assert bp.admit("s", "st", Backlog(2, 1.0)).decision == "accept"
    adm = bp.admit("s", "st", Backlog(3, 1.0))
    assert adm.decision == "coalesce" and adm.underived and adm.reason == "items"
    assert bp.admit("s", "st", Backlog(1, 11.0)).reason == "age"
    with pytest.raises(BackpressureRejected) as ei:
        bp.admit("s", "st", Backlog(5, 1.0))
    assert ei.value.receipt.reason == "items" and ei.value.receipt.limit == 5
    with pytest.raises(BackpressureRejected) as ei2:
        bp.admit("s", "st", Backlog(1, 101.0))
    assert ei2.value.receipt.reason == "age"
    with pytest.raises(BackpressureRejected):
        Backpressure(max_total_items=2).admit("s", "st", Backlog(0, 0.0), total_items=2)


def test_zero_disables_a_cap() -> None:
    bp = Backpressure(coalesce_items=0, max_items=0, max_age_s=0)
    assert bp.admit("s", "st", Backlog(10**6, 10**6)).decision == "accept"
    with pytest.raises(ValueError):
        Backpressure(max_items=-1)


# ------------------------------------------------------------------------------ runner + outbox
def _runner(engine: Any = None, clock: Any = None, **kw: Any) -> InlineRunner:
    return InlineRunner(
        SqlJobStore(engine or memory_sync_engine()),
        clock=clock or ManualClock(T0),
        budget=RetryBudget(base_backoff=timedelta(0)),
        **kw,
    )


class Handler:
    def __init__(self) -> None:
        self.done: list[int] = []
        self.priorities: list[Priority] = []
        self.payloads: list[dict[str, Any]] = []

    async def prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        self.priorities.append(current_priority())
        self.payloads.append(payload)
        return payload

    async def commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        self.done.append(offset)


async def test_coalesce_under_load_drops_nothing_and_every_job_finishes() -> None:
    ledger = SloLedger()
    runner = _runner()
    h = Handler()
    runner.register("derive", prepare=h.prepare, commit=h.commit)
    runner.configure_scheduler(Backpressure(coalesce_items=4, max_items=1000), ledger)
    handles = [await runner.enqueue(SCOPE, "derive", {"i": i}) for i in range(30)]
    # the book keeps ONE queued follow-up per scope and stage: the rest ride it
    assert sum(1 for x in handles if x.state == "coalesced") == 29
    assert runner.depth() == 1
    assert runner.store.backlog(SCOPE, "derive")[0] == 30
    await runner.drain()
    assert sorted(h.done) == list(range(30)) and runner.store.backlog()[0] == 0
    marked = [p["i"] for p in h.payloads if p.get(UNDERIVED_KEY)]
    assert marked == list(range(4, 30))  # from the soft cap on, marked underived
    assert ledger.coalesced == 26 and ledger.rejected == 0
    assert all(p is Priority.DERIVE for p in h.priorities)
    assert ledger.report().lag_samples == 30


async def test_reject_before_accept_then_everything_eventually_done() -> None:
    tap.configure(TapConfig(enabled=True, sink="memory"))
    ledger = SloLedger()
    runner = _runner()
    h = Handler()
    runner.register("derive", prepare=h.prepare, commit=h.commit)
    runner.configure_scheduler(
        Backpressure(coalesce_items=2, max_items=5, retry_after_s=0.5), ledger
    )
    accepted: list[int] = []
    held: list[int] = []
    with tap.request("write"):
        for i in range(12):
            try:
                await runner.enqueue(SCOPE, "derive", {"i": i})
                accepted.append(i)
            except BackpressureRejected as exc:
                held.append(i)  # the caller keeps it
                assert exc.receipt.retry_after_s == 0.5 and exc.receipt.items == 5
    assert accepted == list(range(5)) and held == list(range(5, 12))
    # rejected BEFORE accept: no row, no job, no offset was used for them
    assert runner.store.last_offset(SCOPE, "derive") == 4 and ledger.rejected == 7
    names = [e["stage"] for tr in tap.memory_traces() for e in tr["events"]]
    assert names.count("runtime.backpressure.reject") == 7
    assert "runtime.backpressure.coalesce" in names

    await runner.drain()  # background catches up; the held work is offered again
    for i in held[:5]:
        await runner.enqueue(SCOPE, "derive", {"i": i})
    await runner.drain()
    for i in held[5:]:
        await runner.enqueue(SCOPE, "derive", {"i": i})
    await runner.drain()
    assert len(h.done) == 12 and sorted(p["i"] for p in h.payloads) == list(range(12))


async def test_age_cap_uses_the_clock_and_force_accepts_past_the_hard_cap() -> None:
    clock = ManualClock(T0)
    runner = _runner(clock=clock)
    h = Handler()
    runner.register("derive", prepare=h.prepare, commit=h.commit)
    runner.configure_scheduler(Backpressure(max_age_s=30.0))
    await runner.enqueue(SCOPE, "derive", {"i": 0})
    clock.advance(31)
    with pytest.raises(BackpressureRejected) as ei:
        await runner.enqueue(SCOPE, "derive", {"i": 1})
    assert ei.value.receipt.reason == "age"
    await runner.enqueue(SCOPE, "derive", {"i": 1}, force=True)  # an explicit flush defers nothing
    await runner.drain()
    assert h.done == [0, 1] and h.payloads[1].get(UNDERIVED_KEY) is True


async def test_underived_markers_survive_a_restart(tmp_path: Path) -> None:
    from memspine.clients.sqlite.sync import sqlite_sync_engine

    path = str(tmp_path / "o.db")
    clock = ManualClock(T0)
    first = _runner(sqlite_sync_engine(path, busy_timeout_s=5.0), clock)
    first.register("derive", prepare=Handler().prepare, commit=Handler().commit)
    for i in range(3):
        await first.enqueue(SCOPE, "derive", {"i": i})
    first.close()
    clock.advance(120)  # nobody was running
    again = _runner(sqlite_sync_engine(path, busy_timeout_s=5.0), clock)
    items, oldest = again.store.backlog()
    assert items == 3 and oldest is not None
    ledger = SloLedger()
    ledger.set_backlog(items, (clock() - oldest).total_seconds(), restart_tail_s=120.0)
    rep = ledger.report()
    assert rep.backlog_items == 3 and rep.backlog_age_s == pytest.approx(120.0)
    h = Handler()
    again.register("derive", prepare=h.prepare, commit=h.commit)
    await again.start()
    assert h.done == [0, 1, 2] and again.store.backlog()[0] == 0


# ------------------------------------------------------------------------------ SLO ledger
def test_percentile_and_report_budgets() -> None:
    assert percentile([], 0.95) == 0.0
    assert percentile([float(i) for i in range(1, 101)], 0.95) == 95.0
    ledger = SloLedger(read_p95_budget_s=0.1, lag_p95_budget_s=1.0)
    for v in (0.01, 0.02, 0.5):
        ledger.record_read(v)
    ledger.record_lag(0.2)
    rep = ledger.report()
    assert rep.read_p95_s == 0.5 and rep.read_ok is False and rep.lag_ok is True


async def test_timed_read_records_the_outermost_call_only() -> None:
    ledger = SloLedger()
    with bindings.bound(bindings.EngineBinding(slo_ledger=ledger)):

        @timed_read
        async def inner() -> None:
            await asyncio.sleep(0.002)

        @timed_read
        async def outer() -> None:
            await inner()

        await outer()
        await inner()
    assert ledger.report().reads == 2


# ------------------------------------------------------------------------------ the load test
async def _load(prioritised: bool) -> tuple[float, int, int]:
    """Reads arrive every 15 ms against a saturating background derive burst; returns the read
    p95 (s), background calls done and background calls issued."""
    gate = ModelGate(2, background_reserve=1 if prioritised else 0)
    lat: list[float] = []
    done = 0
    bg_total = 40
    bg_class = Priority.DERIVE if prioritised else Priority.READ

    async def bg() -> None:
        nonlocal done
        async with gate.slot(bg_class):
            await asyncio.sleep(0.02)  # stub slow model call
            done += 1

    async def read() -> None:
        t0 = time.perf_counter()
        async with gate.slot(Priority.READ):
            await asyncio.sleep(0.01)
        lat.append(time.perf_counter() - t0)

    bgs = [asyncio.create_task(bg()) for _ in range(bg_total)]
    reads = []
    for _ in range(30):
        reads.append(asyncio.create_task(read()))
        await asyncio.sleep(0.015)
    await asyncio.gather(*reads, *bgs)
    return percentile(lat, 0.95), done, bg_total


async def test_load_reads_keep_their_p95_under_background_load_and_nothing_is_dropped() -> None:
    p95, done, total = await _load(prioritised=True)
    assert done == total  # every background call finished, none dropped
    assert p95 < 0.06, p95  # one read call is 10 ms; a read never waits behind the burst
    baseline, done_b, total_b = await _load(prioritised=False)
    assert done_b == total_b
    assert baseline > p95 * 2  # without classes the same reads queue behind the burst


async def test_load_through_the_runner_reads_stay_fast_and_all_jobs_finish() -> None:
    cfg = _runtime(scheduler_v2=True, model_concurrency=2, backlog_max_items=0)
    lease = await sched.acquire(cfg)
    assert lease is not None
    s = lease.value
    seams = contextlib.ExitStack()
    seams.enter_context(bindings.bound(bindings.EngineBinding(gate=s.gate)))
    try:
        runner = _runner()

        async def prepare(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
            async with model_slot("llm"):
                await asyncio.sleep(0.02)

        async def commit(scope: OwnerScope, offset: int, prepared: Any) -> None:
            return None

        runner.register("derive", prepare=prepare, commit=commit)
        runner.configure_scheduler(s.backpressure, s.ledger)
        for i in range(16):
            await runner.enqueue(SCOPE, "derive", {"i": i})
        drain = asyncio.create_task(runner.drain())
        lat: list[float] = []
        for _ in range(12):
            t0 = time.perf_counter()
            async with model_slot("llm"):  # a read's model call, class READ
                await asyncio.sleep(0.005)
            lat.append(time.perf_counter() - t0)
            await asyncio.sleep(0.01)
        report = await drain
        assert report.items == 16 and runner.store.backlog()[0] == 0
        assert percentile(lat, 0.95) < 0.05
        assert s.ledger.report().lag_samples == 16
    finally:
        seams.close()
        await sched.release(lease)


# ------------------------------------------------------------------------------ engine, flag on
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))


async def test_engine_flag_on_gives_the_same_facts_and_releases_the_gate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from test_outbox_windows import FakeWindowLLM, _contents, _facts, _send, _turns

    from memspine import Engine
    from memspine.services.llm.base import LLMRouter

    async def run(on: bool) -> list[str]:
        llm = FakeWindowLLM()

        async def router(
            config: MemspineConfig, hooks: object = None, wiring: object = None
        ) -> LLMRouter:
            return LLMRouter({"window_extract": llm})  # type: ignore[dict-item]

        monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
        runtime: dict[str, Any] = (
            {
                "scheduler_v2": True,
                "model_concurrency": 2,
                "backlog_coalesce_items": 1,
                "backlog_max_items": 2,  # tiny: forces coalescing, rejection and deferral
                "backlog_retry_after_s": 0.01,
            }
            if on
            else {}
        )
        eng = Engine(
            template="core",
            dotenv_path=None,
            storage={
                "records": {
                    "provider": "sqlite",
                    "settings": {"path": str(tmp_path / f"{on}.db"), "busy_timeout_s": 5.0},
                }
            },
            model={
                "embedding": {"provider": "hash", "settings": {}},
                "llm": {
                    "window_extract": {
                        "provider": "litellm",
                        "timeout_s": 20.0,
                        "settings": {"model": "fake/window"},
                    }
                },
            },
            memories={"episodic": {"enabled": True}, "semantic": {"enabled": True}},
            write={"window_extract": "facts", "window_async": False, "window_idle_s": 0.0},
            workers={"outbox": True},
            runtime=runtime,
        )
        await eng.start()
        try:
            with bindings.bound(eng._binding):  # what this Engine's calls see
                assert (installed_gate() is not None) is on
            for ns in ("a", "b"):
                await _send(eng, ns, "s1", _turns("trip", 9))
                await _send(eng, ns, "s2", _turns("walk", 4))
            await eng.flush_windows()
            assert eng.window_stats()["queued"] == 0
            if on:
                led = eng._windows._outbox._ledger  # the shared SLO ledger
                assert led.coalesced + led.rejected > 0  # the tiny caps really bit
            return _contents(await _facts(eng, "a") + await _facts(eng, "b"))
        finally:
            await eng.stop()
            assert installed_gate() is None

    off = await run(False)
    on = await run(True)
    assert off and off == on  # nothing dropped, nothing duplicated, same facts
    assert re.search(r".", on[0])
