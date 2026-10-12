from __future__ import annotations

import pytest

from memspine.runtime.jobs import InMemoryJobStore, JobBook, ManualClock
from memspine.runtime.quotas import QuotaPolicy, QuotaRequest, RoundRobinPicker, TokenBucket
from memspine.schemas.durable.common import OwnerScope

SCOPE = OwnerScope(tenant="t", owner="u")


def test_storage_and_queue_rejection_typed():
    clock = ManualClock()
    gate = QuotaPolicy(max_storage_bytes=100, max_queue_depth=2).gate(clock)
    assert (
        gate.check(QuotaRequest("s", storage_current=50, storage_add=50, queue_current=1)) is None
    )
    r = gate.check(QuotaRequest("s", storage_current=50, storage_add=51))
    assert r.quota == "storage" and r.limit == 100 and r.current == 50 and r.retry_after is None
    r = gate.check(QuotaRequest("s", queue_current=2))
    assert r.quota == "queue_depth" and r.limit == 2 and r.current == 2


def test_rate_token_bucket_with_injected_clock_and_retry_after():
    clock = ManualClock()
    gate = QuotaPolicy(rate=3, rate_window_seconds=30).gate(clock)  # 0.1 token/s
    for _ in range(3):
        assert gate.check(QuotaRequest("s")) is None
    r = gate.check(QuotaRequest("s"))
    assert r.quota == "rate" and r.limit == 3 and r.retry_after == pytest.approx(10.0)
    clock.advance(10)
    assert gate.check(QuotaRequest("s")) is None
    assert gate.check(QuotaRequest("other")) is None  # per-scope bucket
    clock.advance(10_000)
    assert TokenBucket(3, 0.1, clock).tokens == 3  # capped at capacity


def test_rejection_consumes_nothing():
    clock = ManualClock()
    gate = QuotaPolicy(max_queue_depth=1, rate=2).gate(clock)
    for _ in range(5):
        assert gate.check(QuotaRequest("s", queue_current=1)).quota == "queue_depth"
    assert gate.check(QuotaRequest("s")) is None and gate.check(QuotaRequest("s")) is None


def test_rate_cost_above_capacity_never_retryable():
    gate = QuotaPolicy(rate=2).gate(ManualClock())
    assert gate.check(QuotaRequest("s", cost=5)).retry_after is None


def test_unlimited_policy_accepts_all_and_bad_policy_rejected():
    assert QuotaPolicy().gate(ManualClock()).check(QuotaRequest("s", storage_add=10**12)) is None
    with pytest.raises(ValueError):
        QuotaPolicy(max_queue_depth=-1)


def test_quota_rejected_before_acceptance_no_job_created():
    """Wiring pattern (done later by RT-02): check first, enqueue only if accepted."""
    clock = ManualClock()
    book = JobBook(InMemoryJobStore(), clock)
    gate = QuotaPolicy(max_queue_depth=2).gate(clock)
    accepted, rejected = [], []
    for off in range(5):
        req = QuotaRequest(("t", "u"), queue_current=book.queue_depth(SCOPE))
        err = gate.check(req)
        if err is None:
            accepted.append(book.enqueue(SCOPE, f"stage{off}", off))
        else:
            rejected.append(err)
    assert len(accepted) == 2 and len(rejected) == 3
    assert book.queue_depth(SCOPE) == 2  # rejected requests left no job behind
    assert all(a.state == "queued" for a in accepted)


def test_round_robin_picker_cycles_and_oldest_within_scope():
    p = RoundRobinPicker()
    assert [p.next_scope(["b", "a", "c"]) for _ in range(4)] == ["a", "b", "c", "a"]
    assert p.next_scope(["a"]) == "a"
    with pytest.raises(ValueError):
        p.next_scope([])
