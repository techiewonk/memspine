"""RM-159: indexed job-store queries, an atomic claim and bounded job-table growth.

The per-write cost is proven by counting SQL statements and deserialized job rows as the store
grows; nothing here measures wall-clock time."""

from __future__ import annotations

import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import event
from sqlalchemy.dialects import postgresql, sqlite
from sqlalchemy.exc import IntegrityError
from sqlalchemy.schema import CreateIndex

from memspine.clients.sqlite.sync import memory_sync_engine, sqlite_sync_engine
from memspine.config.constants import OUTBOX_FINISHED_JOBS_KEPT
from memspine.observability import tap
from memspine.runtime import outbox as outbox_module
from memspine.runtime.jobs import Job, JobBook, ManualClock, RetryBudget, scope_key
from memspine.runtime.outbox import SqlJobStore
from memspine.runtime.runner import InlineRunner, StageHandler
from memspine.schemas.durable.common import OwnerScope
from memspine.services.storage.outbox_tables import outbox

SCOPE = OwnerScope(tenant="t", owner="ns")
T0 = datetime(2026, 11, 2, tzinfo=UTC)
TTL = timedelta(minutes=5)


@pytest.fixture(autouse=True)
def _tap_off():
    tap.shutdown()
    yield
    tap.shutdown()


class Items:
    def __init__(self, fail: set[int] | None = None) -> None:
        self.fail = fail or set()
        self.committed: list[int] = []

    async def prepare(self, scope: OwnerScope, offset: int, payload: dict[str, Any]) -> Any:
        if offset in self.fail:
            raise RuntimeError(f"boom at {offset}")
        return payload

    async def commit(self, scope: OwnerScope, offset: int, prepared: Any) -> None:
        self.committed.append(offset)


def _runner(engine: Any, items: Items | None = None) -> InlineRunner:
    runner = InlineRunner(
        SqlJobStore(engine), clock=ManualClock(T0), budget=RetryBudget(base_backoff=timedelta(0))
    )
    items = items or Items()
    runner.register("derive", StageHandler(items.prepare, items.commit))
    return runner


def _file_engine(tmp_path: Path, name: str = "outbox.db") -> Any:
    return sqlite_sync_engine(str(tmp_path / name), busy_timeout_s=10.0)


class Meter:
    """Counts SQL statements on one engine and job rows deserialized by the store."""

    def __init__(self, engine: Any, monkeypatch: pytest.MonkeyPatch) -> None:
        self.statements = 0
        self.rows = 0
        event.listen(engine, "after_cursor_execute", self._on_execute)
        original = outbox_module._row_to_job

        def counting(r: Any) -> Job:
            self.rows += 1
            return original(r)

        monkeypatch.setattr(outbox_module, "_row_to_job", counting)

    def _on_execute(self, *args: Any) -> None:
        self.statements += 1

    def reset(self) -> tuple[int, int]:
        out = (self.statements, self.rows)
        self.statements = self.rows = 0
        return out


async def _write(runner: InlineRunner, n: int) -> None:
    """One write: three items (the last two coalesce into the first's job), then a drain."""
    for i in range(3):
        await runner.enqueue(SCOPE, "derive", {"n": n, "i": i})
    await runner.drain(SCOPE)


# --------------------------------------------------------------------------- constant cost
async def test_per_write_job_store_work_is_constant_as_the_store_grows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _file_engine(tmp_path)
    runner = _runner(engine)
    meter = Meter(engine, monkeypatch)
    samples: dict[int, tuple[int, int]] = {}
    for n in range(240):
        meter.reset()
        await _write(runner, n)
        samples[n] = meter.reset()
    # Steady state reached once the retention window is full: the same statements and the
    # same rows at write 60 as at write 239, although four times as many messages came in.
    assert samples[60] == samples[120] == samples[239]
    statements, rows = samples[239]
    assert rows <= 12  # the live job, its claim and its coalesced records; never the table
    total = len(runner.store.jobs())
    assert total <= OUTBOX_FINISHED_JOBS_KEPT + 4, total  # 720 jobs were written
    assert runner.book.progress(SCOPE, "derive").watermark == 3 * 240 - 1
    runner.close()


# --------------------------------------------------------------------------- atomic claim
def _concurrent_claims(books: list[JobBook]) -> list[Any]:
    barrier = threading.Barrier(len(books))
    results: list[Any] = [None] * len(books)

    def claim(i: int) -> None:
        barrier.wait()
        results[i] = books[i].claim(f"worker-{i}", TTL)

    threads = [threading.Thread(target=claim, args=(i,)) for i in range(len(books))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    return results


def test_two_claimers_on_two_engines_exactly_one_wins(tmp_path: Path) -> None:
    stores = [SqlJobStore(_file_engine(tmp_path)), SqlJobStore(_file_engine(tmp_path))]
    clock = ManualClock(T0)
    books = [JobBook(s, clock, RetryBudget(base_backoff=timedelta(0))) for s in stores]
    for round_ in range(12):
        scope = OwnerScope(tenant="t", owner=f"ns-{round_}")
        books[0].enqueue(scope, "derive", 0)
        results = _concurrent_claims(books)
        winners = [r for r in results if r is not None]
        assert len(winners) == 1, results
        assert winners[0].lease.epoch == 1
        running = [j for j in stores[0].live_jobs(scope_key(scope)) if j.state == "running"]
        assert [j.job_id for j in running] == [winners[0].job.job_id]
    for s in stores:
        s.close()


def test_two_claimers_never_hold_two_jobs_of_one_key(tmp_path: Path) -> None:
    """A frozen retry and its follow-up are both queued: only one may run."""
    stores = [SqlJobStore(_file_engine(tmp_path)), SqlJobStore(_file_engine(tmp_path))]
    clock = ManualClock(T0)
    books = [JobBook(s, clock, RetryBudget(base_backoff=timedelta(0))) for s in stores]
    books[0].enqueue(SCOPE, "derive", 0)
    first = books[0].claim("w", TTL)
    assert first is not None
    assert books[0].fail(first.job.job_id, first.lease.epoch, "transient").ok  # back to queued
    books[0].enqueue(SCOPE, "derive", 3)  # a second queued job of the same key
    assert len(stores[0].live_jobs(scope_key(SCOPE), "derive")) == 2
    results = _concurrent_claims(books)
    winners = [r for r in results if r is not None]
    assert len(winners) == 1 and winners[0].job.job_id == first.job.job_id  # FIFO: the retry
    for s in stores:
        s.close()


def test_the_database_refuses_a_second_running_job_of_one_key() -> None:
    store = SqlJobStore(memory_sync_engine())
    book = JobBook(store, ManualClock(T0))
    book.enqueue(SCOPE, "derive", 0)
    claim = book.claim("w", TTL)
    assert claim is not None
    rogue = Job(
        job_id="job-rogue",
        seq=99,
        scope=SCOPE,
        stage="derive",
        target_offset=5,
        state="running",
        created_at=T0,
        epoch=1,
    )
    with pytest.raises(IntegrityError):
        store.put_job(rogue)


def test_a_stale_expiry_never_overwrites_a_reclaimed_job(tmp_path: Path) -> None:
    clock = ManualClock(T0)
    a, b = SqlJobStore(_file_engine(tmp_path)), SqlJobStore(_file_engine(tmp_path))
    book_a, book_b = JobBook(a, clock), JobBook(b, clock)
    book_a.enqueue(SCOPE, "derive", 0)
    first = book_a.claim("w1", timedelta(seconds=1))
    assert first is not None
    clock.advance(5)
    [stale] = b.expired_leases(clock())  # process B reads the expired row ...
    again = book_a.claim("w2", TTL)  # ... process A expires and re-claims it first
    assert again is not None and again.lease.epoch == 2
    assert book_b._retry_or_fail(stale, "lease expired", "raised", True, expect=True) is None
    job = a.get_job(again.job.job_id)
    assert job is not None and job.state == "running" and job.epoch == 2
    a.close()
    b.close()


def test_claim_statement_compiles_for_postgres_and_sqlite() -> None:
    stmt = SqlJobStore.claim_statement(
        "w", T0, T0 + TTL, scope=scope_key(SCOPE), stage="derive", exclude=[(("t", "x", "u"), "s")]
    )
    pg = str(stmt.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in pg and "RETURNING" in pg and "LIMIT" in pg
    lite = str(stmt.compile(dialect=sqlite.dialect()))
    assert "FOR UPDATE" not in lite and "RETURNING" in lite
    index = next(i for i in outbox.indexes if i.name == "ux_outbox_running_key")
    ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
    assert "CREATE UNIQUE INDEX" in ddl and "WHERE state = 'running'" in ddl
    ddl = str(CreateIndex(index).compile(dialect=sqlite.dialect()))
    assert "CREATE UNIQUE INDEX" in ddl and "WHERE state = 'running'" in ddl


# --------------------------------------------------------------------------- pruning
async def test_pruning_keeps_the_job_that_recorded_an_open_hole(tmp_path: Path) -> None:
    items = Items(fail={4})
    runner = _runner(_file_engine(tmp_path), items)
    store = runner.store
    for n in range(40):
        await _write(runner, n)
    holes = runner.book.progress(SCOPE, "derive").open_holes
    assert [h.offset for h in holes] == [4]
    assert store.prune_finished(SCOPE, "derive", keep=0) >= 0
    kept = store.jobs()
    covering = [j for j in kept if j.state != "coalesced" and j.target_offset >= 4]
    assert min(j.target_offset for j in covering) == 5  # the job over offsets 3..5 stays
    # waiving the hole releases it
    runner.book.waive_hole(SCOPE, "derive", 4)
    store.prune_finished(SCOPE, "derive", keep=0)
    assert all(j.target_offset != 5 for j in store.jobs())
    runner.close()


async def test_pruning_never_passes_the_watermark_or_a_live_job() -> None:
    store = SqlJobStore(memory_sync_engine())
    book = JobBook(store, ManualClock(T0), RetryBudget(base_backoff=timedelta(0)))
    book.enqueue(SCOPE, "derive", 0)
    first = book.claim("w", TTL)
    assert first is not None
    book.enqueue(SCOPE, "derive", 1)  # follow-up, queued
    coalesced = book.enqueue(SCOPE, "derive", 2)  # coalesces into the follow-up (target 2)
    assert coalesced.state == "coalesced"
    assert book.commit(first.job.job_id, first.lease.epoch).ok  # watermark 0
    store.prune_finished(SCOPE, "derive", keep=0)
    left = {j.job_id: j for j in store.jobs()}
    assert coalesced.job_id in left  # its target 2 is past the watermark
    assert coalesced.coalesced_into in left  # live
    assert first.job.job_id not in left  # finished, at the watermark


async def test_pruning_never_reuses_a_job_id() -> None:
    store = SqlJobStore(memory_sync_engine())
    book = JobBook(store, ManualClock(T0), RetryBudget(base_backoff=timedelta(0)))
    seen: set[str] = set()
    for n in range(6):
        handle = book.enqueue(SCOPE, "derive", n)
        assert handle.job_id not in seen
        seen.add(handle.job_id)
        claim = book.claim("w", TTL)
        assert claim is not None
        assert book.commit(claim.job.job_id, claim.lease.epoch).ok
        store.prune_finished(SCOPE, "derive", keep=0)
        assert [j.job_id for j in store.jobs()] == [handle.job_id]  # the newest row stays
    with pytest.raises(ValueError):
        store.prune_finished(SCOPE, "derive", keep=-1)


async def test_a_stale_worker_naming_a_pruned_job_is_refused() -> None:
    store = SqlJobStore(memory_sync_engine())
    book = JobBook(store, ManualClock(T0), RetryBudget(base_backoff=timedelta(0)))
    book.enqueue(SCOPE, "derive", 0)
    old = book.claim("w", TTL)
    assert old is not None and book.commit(old.job.job_id, old.lease.epoch).ok
    book.enqueue(SCOPE, "derive", 1)  # a newer row, so the old one may go
    assert store.prune_finished(SCOPE, "derive", keep=0) == 1
    refused = book.commit(old.job.job_id, old.lease.epoch)
    assert not refused.ok and refused.reason == "unknown job"
