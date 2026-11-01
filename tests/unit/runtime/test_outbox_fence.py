"""RM-141: the effect door is a fence, not two snapshots.

Each scenario interleaves deterministically (asyncio events, or a patched hook inside the door
transaction); nothing sleeps."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from memspine.clients.sqlite.sync import memory_sync_engine, sqlite_sync_engine
from memspine.observability import tap
from memspine.runtime.jobs import ManualClock, RetryBudget
from memspine.runtime.outbox import EffectFence, SqlJobStore
from memspine.runtime.runner import InlineRunner, StageHandler, _Fenced
from memspine.schemas.durable.common import OwnerScope

SCOPE = OwnerScope(tenant="t", owner="ns")
T0 = datetime(2026, 10, 13, tzinfo=UTC)
SOURCE = {"record_id": "r1", "source_version": 1, "content_hash": "hash-1"}


@pytest.fixture(autouse=True)
def _tap_off():
    tap.shutdown()
    yield
    tap.shutdown()


def _runner(engine: Any = None) -> InlineRunner:
    return InlineRunner(
        SqlJobStore(engine or memory_sync_engine()),
        clock=ManualClock(T0),
        budget=RetryBudget(base_backoff=timedelta(0)),
    )


class FactStore:
    """A provider projection: facts keyed by the source records they came from."""

    def __init__(self) -> None:
        self.facts: dict[int, list[str]] = {}
        self.commits = 0
        self.compensated: list[tuple[int, tuple[str, ...]]] = []
        self.projected = asyncio.Event()
        self.release = asyncio.Event()
        self.hold = False

    async def prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        return list(payload.get("record_ids", []))

    async def commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        self.commits += 1
        self.facts[offset] = list(prepared)
        self.projected.set()
        if self.hold:
            await self.release.wait()

    async def compensate(
        self,
        scope: OwnerScope,
        offset: int,
        prepared: Any,
        effects: Sequence[dict[str, Any]],
    ) -> None:
        self.facts.pop(offset, None)
        self.compensated.append((offset, tuple(e["effect_id"] for e in effects)))

    def handler(self, **kw: Any) -> StageHandler:
        return StageHandler(
            self.prepare,
            self.commit,
            effect_results=kw.pop("effect_results", lambda prepared: {"facts": 1}),
            compensate=self.compensate,
            **kw,
        )


# --------------------------------------------------------------------------- A
async def test_forget_during_commit_leaves_no_erased_fact() -> None:
    runner = _runner()
    facts = FactStore()
    facts.hold = True
    runner.register("derive", facts.handler())
    runner.store.mark_sources(SCOPE, "derive", [SOURCE], T0)
    await runner.enqueue(SCOPE, "derive", {"record_ids": ["r1"]})

    task = asyncio.create_task(runner.drain())
    await asyncio.wait_for(facts.projected.wait(), 5)  # authorized, projection deposited
    assert facts.facts == {0: ["r1"]}
    assert await runner.erase_source(SCOPE, "derive", "r1")  # forget lands mid-commit
    facts.release.set()
    report = await asyncio.wait_for(task, 5)

    assert facts.facts == {}  # the erased fact was compensated away
    assert len(facts.compensated) == 1 and facts.compensated[0][0] == 0
    assert report.fenced == 1 and report.holes == 0
    assert runner.store.effect_status(SCOPE) == []
    assert runner.book.progress(SCOPE, "derive").watermark == -1


async def test_erase_cannot_commit_between_authorize_check_and_write(tmp_path: Path) -> None:
    """The erase is attempted from another connection while the door's transaction is open,
    after its source check: it must serialise after the door, then remove the door's rows."""
    db = str(tmp_path / "outbox.db")
    store = SqlJobStore(sqlite_sync_engine(db, busy_timeout_s=10.0))
    runner = InlineRunner(store, clock=ManualClock(T0))
    store.mark_sources(SCOPE, "derive", [SOURCE], T0)
    await runner.enqueue(SCOPE, "derive", {"record_ids": ["r1"]})
    claim = runner.book.claim("worker", timedelta(minutes=5))
    assert claim is not None
    [snapshot] = store.source_snapshots(SCOPE, "derive", ["r1"])
    effect = {
        "effect_id": "race000000000001",
        "layer": "facts",
        "identity": {"effect_id": "race000000000001", "sources": [snapshot]},
        "count": 1,
        "artifact": None,
    }
    other = SqlJobStore(sqlite_sync_engine(db, busy_timeout_s=10.0))
    attempted = threading.Event()
    erased: list[bool] = []

    def erase() -> None:
        attempted.set()
        erased.append(other.erase_source(SCOPE, "derive", "r1"))

    original = SqlJobStore._sources_current
    eraser = threading.Thread(target=erase)

    def check_then_race(c: Any, scope: Any, stage: str, sources: Any) -> bool:
        ok = original(c, scope, stage, sources)
        if sources and not eraser.is_alive() and not erased:
            eraser.start()
            assert attempted.wait(5)
        return ok

    SqlJobStore._sources_current = staticmethod(check_then_race)  # type: ignore[method-assign]
    try:
        door = store.authorize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            scope=SCOPE,
            stage="derive",
            offset=0,
            effects=[effect],
            sources=[snapshot],
            at=T0,
        )
    finally:
        SqlJobStore._sources_current = staticmethod(original)  # type: ignore[method-assign]
    eraser.join(10)
    assert door is EffectFence.AUTHORIZED and erased == [True]
    assert store.effect_status(SCOPE) == []  # the later erase removed the authorization
    assert (
        store.finalize_effects(
            job_id=claim.job.job_id,
            epoch=claim.lease.epoch,
            effects=[effect],
            failure=None,
            at=T0,
        )
        is EffectFence.STALE_SOURCE
    )
    other.close()
    store.close()


# --------------------------------------------------------------------------- B
async def test_an_erased_source_is_not_reactivated() -> None:
    store = SqlJobStore(memory_sync_engine())
    store.mark_sources(SCOPE, "derive", [SOURCE], T0)
    assert store.erase_source(SCOPE, "derive", "r1")
    assert store.mark_sources(SCOPE, "derive", [SOURCE], T0) == ["r1"]
    assert not store.has_source(SCOPE, "derive", "r1")
    assert store.source_snapshots(SCOPE, "derive", ["r1"]) == []
    # A stale (pre-erasure) generation is refused too; only the current one re-accepts.
    assert store.mark_sources(SCOPE, "derive", [{**SOURCE, "erasure_generation": 0}], T0) == ["r1"]
    assert store.mark_sources(SCOPE, "derive", [{**SOURCE, "erasure_generation": 1}], T0) == []
    [snapshot] = store.source_snapshots(SCOPE, "derive", ["r1"])
    assert snapshot["erasure_generation"] == 1


# --------------------------------------------------------------------------- C
async def test_a_fence_is_not_a_hole_and_holds_the_watermark() -> None:
    runner = _runner()
    facts = FactStore()
    runner.register("derive", facts.handler())
    runner.store.mark_sources(
        SCOPE, "derive", [SOURCE, {**SOURCE, "record_id": "r2"}, {**SOURCE, "record_id": "r3"}], T0
    )
    for rid in ("r1", "r2", "r3"):
        await runner.enqueue(SCOPE, "derive", {"record_ids": [rid]})
    assert runner.store.erase_source(SCOPE, "derive", "r2")

    report = await runner.drain()
    progress = runner.book.progress(SCOPE, "derive")
    assert report.fenced == 1 and report.holes == 0 and report.released == 1
    assert progress.watermark == 0 and progress.holes == []  # stops before the fenced offset
    assert facts.facts == {0: ["r1"]}

    # Forget rewrites the queued item; the next drain resumes at the fenced offset.
    await runner.rewrite_pending(SCOPE, "derive", lambda p: {**p, "record_ids": []})
    again = await runner.drain()
    assert again.fenced == 0 and again.holes == 0
    assert runner.book.progress(SCOPE, "derive").watermark == 2


async def test_a_fence_while_recording_a_failure_chains_the_original_error() -> None:
    runner = _runner()
    facts = FactStore()

    async def failing(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        [job] = runner.book.live_jobs(SCOPE, "derive")
        runner.book.cancel(job.job_id)  # the lease goes while the model call fails
        raise RuntimeError("model call failed")

    handler = StageHandler(failing, facts.commit, effect_results=lambda prepared: {"facts": 0})
    runner.register("derive", handler)
    await runner.enqueue(SCOPE, "derive", {})
    claim = runner.book.claim("worker", timedelta(minutes=5))
    assert claim is not None
    with pytest.raises(_Fenced) as caught:
        await runner._execute_item(handler, claim.job, claim.lease.epoch, 0)
    assert caught.value.reason == "lease"
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert "model call failed" in str(caught.value.__cause__)


# --------------------------------------------------------------------------- D
async def _complete_once(runner: InlineRunner, facts: FactStore) -> None:
    runner.register("derive", facts.handler())
    runner.store.mark_sources(SCOPE, "derive", [SOURCE], T0)
    await runner.enqueue(SCOPE, "derive", {"record_ids": ["r1"]})
    claim = runner.book.claim("worker", timedelta(minutes=5))
    assert claim is not None
    # The first run projects and acknowledges, then dies before its checkpoint.
    await runner._execute_item(facts.handler(), claim.job, claim.lease.epoch, 0)
    assert runner.book.release(claim.job.job_id, claim.lease.epoch).ok
    [row] = runner.store.effect_status(SCOPE)
    assert row["state"] == "complete" and row["count"] == 1


async def test_a_completed_replay_keeps_the_original_error() -> None:
    runner = _runner()
    facts = FactStore()
    await _complete_once(runner, facts)

    async def failing(scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        raise RuntimeError("replay model call failed")

    runner.register(
        "derive",
        StageHandler(
            failing,
            facts.commit,
            effect_results=lambda prepared: {"facts": 0 if prepared is None else 1},
        ),
    )
    report = await runner.drain()
    [hole] = runner.book.progress(SCOPE, "derive").holes
    assert report.holes == 1
    assert hole.last_error is not None and "replay model call failed" in hole.last_error
    [row] = runner.store.effect_status(SCOPE)
    assert row["state"] == "complete" and row["count"] == 1  # not re-finalized as a failure


async def test_a_replay_with_a_different_layer_set_is_a_typed_fence() -> None:
    runner = _runner()
    facts = FactStore()
    await _complete_once(runner, facts)
    commits = facts.commits
    runner.register(
        "derive",
        facts.handler(effect_results=lambda prepared: {"facts": 1, "summaries": 1}),
    )
    report = await runner.drain()
    assert report.fenced == 1 and report.holes == 0
    assert facts.commits == commits  # the second projection never ran
    assert runner.book.progress(SCOPE, "derive").watermark == -1
    [row] = runner.store.effect_status(SCOPE)
    assert row["layer"] == "facts" and row["state"] == "complete"
