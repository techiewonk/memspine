from __future__ import annotations

import random
from datetime import timedelta

import pytest

from memspine.runtime.jobs import (
    IllegalTransition,
    InMemoryJobStore,
    JobBook,
    ManualClock,
    PurposeLimit,
    RecoveryBudget,
    RetryBudget,
    TriggerConfig,
    Triggers,
    TriggerStats,
    maintenance_due,
    should_run,
)
from memspine.runtime.quotas import QuotaPolicy, QuotaRequest, RoundRobinPicker
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.public import CostRecord, QuotaExceeded

SCOPE = OwnerScope(tenant="t", owner="u")
TTL = timedelta(seconds=60)


def make(budget: RetryBudget | None = None, picker=None):
    clock = ManualClock()
    store = InMemoryJobStore()
    return JobBook(store, clock, budget, picker), clock, store


def test_enqueue_claim_commit_lifecycle():
    book, _, _ = make()
    h = book.enqueue(SCOPE, "sleep", 5)
    assert h.state == "queued" and h.target_offset == 5
    c = book.claim("w1", TTL)
    assert c and c.lease.epoch == 1 and c.job.state == "running"
    assert book.progress(SCOPE, "sleep").target_offset == 5
    assert book.commit(c.job.job_id, 1).ok
    assert book.handle(h.job_id).state == "succeeded"
    p = book.progress(SCOPE, "sleep")
    assert p.watermark == 5 and p.target_offset is None and p.complete


def test_illegal_transitions_raise():
    book, _, _ = make()
    h = book.enqueue(SCOPE, "sleep", 1)
    c = book.claim("w", TTL)
    book.commit(c.job.job_id, c.lease.epoch)
    with pytest.raises(IllegalTransition):
        book.cancel(h.job_id)  # succeeded -> cancelled
    h2 = book.enqueue(SCOPE, "sleep", 2)
    book.cancel(h2.job_id)
    with pytest.raises(IllegalTransition):
        book.cancel(h2.job_id)
    assert book.handle(h2.job_id).state == "cancelled"


def test_fencing_stale_worker_cannot_commit_after_reclaim():
    book, clock, store = make()
    h = book.enqueue(SCOPE, "sleep", 3)
    c1 = book.claim("slow", TTL)
    clock.advance(120)  # lease runs out
    c2 = book.claim("fast", TTL)
    assert c2 and c2.lease.epoch == c1.lease.epoch + 1
    assert c2.job.job_id == h.job_id
    stale = book.commit(c1.job.job_id, c1.lease.epoch)
    assert not stale.ok and stale.reason == "stale epoch"
    assert not book.heartbeat(c1.job.job_id, c1.lease.epoch, TTL).ok
    assert book.progress(SCOPE, "sleep").watermark == -1
    assert book.commit(c2.job.job_id, c2.lease.epoch).ok
    log = store.log()
    assert [e.op for e in log] == ["commit", "heartbeat"]
    assert all(e.presented_epoch == 1 and e.current_epoch == 2 for e in log)
    # a late commit after success is refused too
    assert not book.commit(c2.job.job_id, c2.lease.epoch).ok


def test_heartbeat_extends_lease():
    book, clock, _ = make()
    book.enqueue(SCOPE, "sleep", 1)
    c = book.claim("w", TTL)
    clock.advance(50)
    assert book.heartbeat(c.job.job_id, c.lease.epoch, TTL).ok
    clock.advance(50)
    assert book.expire_leases() == []
    clock.advance(20)
    assert book.expire_leases() == [c.job.job_id]


def test_cancel_running_fences_worker():
    book, _, _ = make()
    h = book.enqueue(SCOPE, "sleep", 1)
    c = book.claim("w", TTL)
    book.cancel(h.job_id)
    assert not book.commit(c.job.job_id, c.lease.epoch).ok
    assert book.handle(h.job_id).state == "cancelled"


def test_coalescing_storm_one_running_one_follow_up():
    book, _, _ = make()
    book.enqueue(SCOPE, "sleep", 0)
    c = book.claim("w", TTL)
    handles = [book.enqueue(SCOPE, "sleep", i) for i in range(1, 1001)]
    states = [h.state for h in handles]
    assert states.count("queued") == 1 and states.count("coalesced") == 999
    live = book.live_jobs(SCOPE, "sleep")
    assert sorted(j.state for j in live) == ["queued", "running"]
    follow = next(j for j in live if j.state == "queued")
    run = next(j for j in live if j.state == "running")
    assert run.target_offset == 0  # the running snapshot never mutates
    assert follow.target_offset == 1000
    assert all(h.coalesced_into == follow.job_id for h in handles if h.state == "coalesced")
    assert book.claim("w2", TTL) is None  # single flight per (scope, stage)
    book.commit(c.job.job_id, c.lease.epoch)
    c2 = book.claim("w2", TTL)
    assert c2.job.job_id == follow.job_id and c2.job.target_offset == 1000


def test_trigger_covered_by_running_target_coalesces_into_running():
    book, _, _ = make()
    book.enqueue(SCOPE, "sleep", 10)
    c = book.claim("w", TTL)
    h = book.enqueue(SCOPE, "sleep", 10)
    assert h.state == "coalesced" and h.coalesced_into == c.job.job_id
    assert len(book.live_jobs(SCOPE, "sleep")) == 1


def test_queued_target_is_frozen_at_first_claim_even_after_retry():
    book, clock, _ = make()
    book.enqueue(SCOPE, "sleep", 4)
    c = book.claim("w", TTL)
    book.fail(c.job.job_id, c.lease.epoch, "boom")
    h = book.enqueue(SCOPE, "sleep", 9)  # retry job is frozen -> new follow-up
    assert h.state == "queued"
    assert book.get(c.job.job_id).target_offset == 4
    clock.advance(3600)
    again = book.claim("w", TTL)
    assert again.job.job_id == c.job.job_id and again.lease.epoch == 2


def test_retry_within_budget_then_terminal_failure_makes_holes():
    book, clock, _ = make(RetryBudget(max_attempts=2, base_backoff=timedelta(seconds=10)))
    book.enqueue(SCOPE, "sleep", 2)
    c = book.claim("w", TTL)
    r = book.fail(c.job.job_id, c.lease.epoch, "e1", "malformed")
    assert r.job.state == "queued" and r.job.attempts == 1
    assert book.claim("w", TTL) is None  # backoff not elapsed
    clock.advance(11)
    c = book.claim("w", TTL)
    r = book.fail(c.job.job_id, c.lease.epoch, "e2", "malformed")
    assert r.job.state == "failed"
    p = book.progress(SCOPE, "sleep")
    assert p.watermark == 2 and [h.offset for h in p.holes] == [0, 1, 2]
    assert p.holes[0].reason == "malformed" and p.holes[0].last_error == "e2"
    assert book.handle(r.job.job_id).error == "e2"


def test_non_retryable_failure_is_terminal():
    book, _, _ = make()
    book.enqueue(SCOPE, "sleep", 0)
    c = book.claim("w", TTL)
    assert (
        book.fail(c.job.job_id, c.lease.epoch, "x", "size_skip", retryable=False).job.state
        == "failed"
    )


def test_expired_lease_counts_attempt_and_eventually_fails():
    book, clock, _ = make(RetryBudget(max_attempts=2))
    h = book.enqueue(SCOPE, "sleep", 1)
    for _ in range(2):
        clock.advance(3600)
        assert book.claim("w", TTL)
        clock.advance(120)
        book.expire_leases()
    assert book.handle(h.job_id).state == "failed"
    assert not book.progress(SCOPE, "sleep").complete


def test_hole_lifecycle():
    book, _, _ = make(RetryBudget(max_hole_attempts=2))
    book.enqueue(SCOPE, "sleep", 5)
    c = book.claim("w", TTL)
    book.commit(c.job.job_id, c.lease.epoch, failed={2: ("raised", "x"), 4: ("truncated", None)})
    p = book.progress(SCOPE, "sleep")
    assert p.watermark == 5 and [(h.offset, h.attempts) for h in p.holes] == [(2, 1), (4, 1)]
    assert not p.complete
    assert [h.offset for h in book.holes_to_retry(SCOPE, "sleep")] == [2, 4]
    book.record_hole_result(SCOPE, "sleep", 2, ok=False, error="again")
    assert book.progress(SCOPE, "sleep").holes[0].last_error == "again"
    assert [h.offset for h in book.holes_to_retry(SCOPE, "sleep")] == [4]  # 2 is out of budget
    assert [h.offset for h in book.progress(SCOPE, "sleep").open_holes] == [2, 4]  # still visible
    book.record_hole_result(SCOPE, "sleep", 4, ok=True)
    book.waive_hole(SCOPE, "sleep", 2)
    p = book.progress(SCOPE, "sleep")
    assert p.complete and [h.offset for h in p.holes] == [2] and p.holes[0].waived
    with pytest.raises(KeyError):
        book.record_hole_result(SCOPE, "sleep", 99, ok=True)


def test_watermark_contiguity_and_checkpoints():
    book, _, _ = make()
    book.enqueue(SCOPE, "sleep", 9)
    c = book.claim("w", TTL)
    e = c.lease.epoch
    assert book.checkpoint(c.job.job_id, e, 3, {1: ("raised", "bad")}).ok
    p = book.progress(SCOPE, "sleep")
    assert p.watermark == 3 and [h.offset for h in p.holes] == [1] and p.target_offset == 9
    with pytest.raises(ValueError):
        book.checkpoint(c.job.job_id, e, 2)  # backwards
    with pytest.raises(ValueError):
        book.checkpoint(c.job.job_id, e, 5, {3: ("raised", None)})  # 3 already behind the watermark
    with pytest.raises(ValueError):
        book.checkpoint(c.job.job_id, e, 10)  # past the snapshot target
    book.commit(c.job.job_id, e)
    p = book.progress(SCOPE, "sleep")
    assert p.watermark == 9 and [h.offset for h in p.holes] == [1]
    # next job starts from the watermark; target below it is a no-op range
    book.enqueue(SCOPE, "sleep", 4)
    c2 = book.claim("w", TTL)
    assert book.commit(c2.job.job_id, c2.lease.epoch).ok
    assert book.progress(SCOPE, "sleep").watermark == 9


def test_restart_persisted_queue_survives_stop():
    book, clock, store = make()
    a = book.enqueue(SCOPE, "sleep", 7)
    b = book.enqueue(OwnerScope(tenant="t", owner="v"), "sleep", 3)
    c = book.claim("w", TTL)
    book.checkpoint(c.job.job_id, c.lease.epoch, 2)
    plan = book.drain_plan(clock() + timedelta(seconds=5), lambda j: 100.0)
    assert plan.release == (c.job.job_id,) and plan.finish == ()
    book.apply_drain_plan(plan)
    # "restart": a fresh book over the same store
    book2 = JobBook(store, clock)
    assert {j.job_id for j in book2.live_jobs()} == {a.job_id, b.job_id}
    assert all(j.state == "queued" for j in book2.live_jobs())
    c2 = book2.claim("w2", TTL)
    assert c2.job.job_id == a.job_id and c2.lease.epoch == 2
    assert book2.progress(SCOPE, "sleep").watermark == 2
    assert not book.commit(c.job.job_id, c.lease.epoch).ok  # old process is fenced
    assert book2.commit(c2.job.job_id, 2).ok
    assert book2.progress(SCOPE, "sleep").watermark == 7


def test_drain_plan_finishes_short_jobs_and_keeps_queue():
    book, clock, _ = make()
    s2 = OwnerScope(tenant="t", owner="v")
    s3 = OwnerScope(tenant="t", owner="w")
    book.enqueue(SCOPE, "sleep", 1)
    book.enqueue(s2, "sleep", 1)
    book.enqueue(s3, "sleep", 1)
    ca = book.claim("a", TTL, scope=SCOPE)
    cb = book.claim("b", TTL, scope=s2)
    est = {ca.job.job_id: 2.0, cb.job.job_id: None}
    plan = book.drain_plan(clock() + timedelta(seconds=10), lambda j: est.get(j.job_id))
    assert plan.finish == (ca.job.job_id,) and plan.release == (cb.job.job_id,)
    assert len(plan.keep_queued) == 1
    book.commit(ca.job.job_id, ca.lease.epoch)
    assert book.apply_drain_plan(plan) == [cb.job.job_id]
    assert book.get(cb.job.job_id).attempts == 0  # release costs no attempt
    assert book.queue_depth() == 2


def test_round_robin_fairness_across_scopes():
    picker = RoundRobinPicker()
    book, _, _ = make(picker=picker)
    scopes = [OwnerScope(tenant="t", owner=o) for o in "abc"]
    for s in scopes:
        for off in (1, 2):
            book.enqueue(s, f"st{off}", off)
    order = []
    for _ in range(6):
        c = book.claim("w", TTL)
        order.append(c.job.scope.owner)
        book.commit(c.job.job_id, c.lease.epoch)
    assert order == ["a", "b", "c", "a", "b", "c"]


def test_recovery_budget_per_source_and_purpose():
    rb = RecoveryBudget(
        {"recovery": PurposeLimit(calls=2, tokens=100), "extraction": PurposeLimit(calls=1)}
    )
    assert rb.try_spend("s1", "recovery", 1, 60)
    assert not rb.try_spend("s1", "recovery", 1, 60)  # tokens would exceed: nothing spent
    assert rb.remaining("s1", "recovery") == (1, 40)
    assert rb.try_spend("s2", "recovery", 2, 0)  # other source unaffected
    assert rb.try_spend("s1", "extraction")
    assert not rb.try_spend("s1", "extraction") and rb.exhausted("s1", "extraction")
    assert rb.try_spend("s1", "other", 99, 99)  # unmetered purpose
    rec = CostRecord(
        cost_class="generative",
        job_id="j",
        model_id="m",
        purpose="recovery",
        tokens_in=30,
        tokens_out=10,
    )
    assert rb.charge("s1", rec) and rb.remaining("s1", "recovery") == (0, 0)
    with pytest.raises(ValueError):
        RecoveryBudget({"bogus": PurposeLimit()})


@pytest.mark.parametrize("seed", range(40))
def test_property_accepted_jobs_never_lost(seed):
    rng = random.Random(seed)
    book, clock, store = make(RetryBudget(max_attempts=3, base_backoff=timedelta(seconds=1)))
    scopes = [OwnerScope(tenant="t", owner=o) for o in "ab"]
    accepted: dict[str, int] = {}  # handle id -> offset it must be covered up to
    claims = []
    next_off = {s.owner: 0 for s in scopes}
    for _ in range(300):
        op = rng.choice(["enq", "enq", "claim", "commit", "fail", "expire", "hb", "adv", "restart"])
        if op == "enq":
            s = rng.choice(scopes)
            next_off[s.owner] += rng.randint(0, 3)
            h = book.enqueue(s, "sleep", next_off[s.owner])
            accepted[h.job_id] = next_off[s.owner]
        elif op == "claim":
            c = book.claim(f"w{rng.randint(0, 3)}", timedelta(seconds=rng.randint(5, 40)))
            if c:
                claims.append((c.job.job_id, c.lease.epoch))
        elif op in ("commit", "fail", "hb") and claims:
            jid, ep = rng.choice(claims)  # may be stale on purpose
            if op == "commit":
                book.commit(jid, ep)
            elif op == "fail":
                book.fail(jid, ep, "x", retryable=rng.random() < 0.8)
            else:
                book.heartbeat(jid, ep, timedelta(seconds=30))
        elif op == "adv":
            clock.advance(rng.randint(0, 30))
        elif op == "expire":
            book.expire_leases()
        else:
            book = JobBook(store, clock, book.budget)
        # invariants
        for key in {(j.key) for j in store.jobs()}:
            live = [j for j in store.jobs() if j.key == key and j.state in ("queued", "running")]
            assert sum(j.state == "running" for j in live) <= 1
            assert sum(j.state == "queued" and not j.frozen for j in live) <= 1
        epochs = {}
        for j in store.jobs():
            epochs[j.job_id] = j.epoch
    # drive everything to completion; every accepted offset must end up behind a watermark
    for _ in range(2000):
        clock.advance(3600)
        book.expire_leases()
        c = book.claim("final", timedelta(seconds=30))
        if not c:
            break
        book.commit(c.job.job_id, c.lease.epoch)
    assert not book.live_jobs()
    for s in scopes:
        p = book.progress(s, "sleep")
        # watermark covers every accepted offset (holes are visible, not skipped)
        want = max([o for jid, o in accepted.items() if store.get_job(jid).scope == s], default=-1)
        assert p.watermark >= want
    for jid in accepted:
        j = store.get_job(jid)
        assert j is not None
        if j.state == "coalesced":
            tgt = store.get_job(j.coalesced_into)
            assert (
                tgt is not None
                and tgt.state != "cancelled"
                and tgt.target_offset >= j.target_offset
            )


# ----------------------------------------------------------------------------- triggers
T0 = ManualClock().__call__()


def test_trigger_deposits_and_idle():
    cfg = TriggerConfig(min_deposits=5, idle=timedelta(minutes=10), min_gap=timedelta(hours=1))
    now = T0 + timedelta(days=1)
    s = TriggerStats(deposits_since_run=5, last_deposit_at=now - timedelta(minutes=3))
    d = should_run(s, now, cfg)
    assert not d.run and d.reason == "idle_wait" and d.rearm_at == s.last_deposit_at + cfg.idle
    d = should_run(TriggerStats(5, now - timedelta(minutes=10)), now, cfg)
    assert d.run and d.kind == "sleep"
    assert not should_run(TriggerStats(4, now - timedelta(hours=1)), now, cfg).run  # N not met
    assert not should_run(
        TriggerStats(0, now - timedelta(hours=1)), now, TriggerConfig(min_deposits=0)
    ).run


def test_trigger_context_pressure_default_70_and_gap():
    cfg = TriggerConfig()
    now = T0 + timedelta(days=1)
    assert cfg.context_pressure == 0.70
    assert not should_run(TriggerStats(context_pressure=0.69), now, cfg).run
    d = should_run(TriggerStats(context_pressure=0.70), now, cfg)
    assert d.run and d.kind == "compression"
    # gap K gates BOTH kinds
    recent = now - timedelta(minutes=5)
    d = should_run(TriggerStats(context_pressure=0.9, last_run_at=recent), now, cfg)
    assert not d.run and d.reason == "gap" and d.rearm_at == recent + cfg.min_gap
    cfg2 = TriggerConfig(min_deposits=1, idle=timedelta(0))
    d = should_run(TriggerStats(3, now, last_run_at=recent), now, cfg2)
    assert not d.run and d.reason == "gap"
    d = should_run(TriggerStats(3, now, last_run_at=now - timedelta(hours=1)), now, cfg2)
    assert d.run
    both = should_run(TriggerStats(3, now, context_pressure=0.8), now, cfg2)
    assert both.kind == "sleep" and "context_pressure" in both.reason


def test_trigger_config_validation_and_maintenance():
    with pytest.raises(ValueError):
        TriggerConfig(context_pressure=0.0)
    with pytest.raises(ValueError):
        TriggerConfig(min_deposits=-1)
    tr = Triggers(TriggerConfig(maintenance_interval=timedelta(hours=24)))
    now = T0 + timedelta(days=2)
    assert tr.maintenance_due(TriggerStats(), now)
    assert not maintenance_due(TriggerStats(last_maintenance_at=now - timedelta(hours=23)), now)
    assert tr.maintenance_due(TriggerStats(last_maintenance_at=now - timedelta(hours=24)), now)
    assert tr.should_run(TriggerStats(context_pressure=1.0), now).run


def test_quota_exceeded_import_is_public_model():
    assert QuotaExceeded.model_fields["quota"] is not None and QuotaPolicy().max_queue_depth is None
    assert QuotaRequest("x").queue_add == 1
