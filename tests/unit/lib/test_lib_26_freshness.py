from __future__ import annotations

from datetime import timedelta

from memspine.runtime.freshness import (
    BackgroundBudget,
    BudgetConfig,
    FreshnessPolicy,
    derived_freshness,
    freshness,
    read_barrier,
)
from memspine.runtime.jobs import ManualClock
from memspine.schemas.durable.common import OwnerScope
from memspine.schemas.durable.jobs import Hole, Progress

S = OwnerScope(tenant="t", owner="u")
S2 = OwnerScope(tenant="t", owner="v")


def prog(wm: int, holes: tuple[int, ...] = (), waived: tuple[int, ...] = ()) -> Progress:
    hs = [Hole(offset=o, reason="raised", waived=o in waived) for o in holes]
    return Progress(scope=S, job_kind="facts", watermark=wm, holes=hs)


def test_fresh_and_lagging_with_holes():
    assert freshness("facts", 9, prog(9)).status == "fresh"
    v = freshness("facts", 9, prog(6, holes=(2, 4)))
    assert (v.status, v.behind, v.holes, v.covers_seq) == ("lagging", 3, (2, 4), 6)
    v = freshness("facts", 9, prog(9, holes=(3,)))
    assert (v.status, v.behind, v.holes) == ("lagging", 0, (3,))
    assert freshness("facts", 9, prog(9, holes=(3,), waived=(3,))).status == "fresh"
    assert freshness("facts", 3, None).behind == 4


def test_stale_by_lag_and_age_with_injected_clock():
    clock = ManualClock()
    adv = clock()
    pol = FreshnessPolicy(max_lag_events=5, max_age=timedelta(minutes=10))
    assert freshness("facts", 9, prog(2), policy=pol).status == "stale"
    p = prog(7)
    assert freshness("facts", 9, p, policy=pol, now=clock(), advanced_at=adv).status == "lagging"
    clock.advance(seconds=601)
    v = freshness("facts", 9, p, policy=pol, now=clock(), advanced_at=adv)
    assert (v.status, v.reason) == ("stale", "max_age")
    # caught up is never stale by age
    assert freshness("facts", 9, prog(9), policy=pol, now=clock(), advanced_at=adv).ok


def test_unconfigured_is_unavailable():
    pol = FreshnessPolicy(configured=frozenset({"facts"}))
    assert freshness("graph", 9, prog(9), policy=pol).status == "unavailable"


def test_derived_freshness_null_covers_is_stale():
    assert derived_freshness(None, 5)[0] is True
    assert derived_freshness(5, 5) == (False, 0)
    assert derived_freshness(3, 5) == (True, 2)


def test_barrier_wait_then_partial_after_timeout():
    clock = ManualClock()
    start = clock()
    progs = {"facts": prog(10), "graph": prog(4), "summaries": None}
    kw = {"timeout": timedelta(seconds=5), "clock": clock, "started_at": start}
    r = read_barrier(["facts", "graph", "summaries"], 10, progs, **kw)
    assert r.status == "wait" and r.wait_until_seq == 10 and not r.may_proceed
    assert r.pending == ("graph", "summaries") and r.ready == ("facts",) and r.missing == ()
    clock.advance(seconds=5)
    r = read_barrier(["facts", "graph", "summaries"], 10, progs, **kw)
    assert r.status == "partial" and r.may_proceed
    assert r.missing == ("graph", "summaries")
    progs["graph"] = prog(12)
    progs["summaries"] = prog(10)
    assert read_barrier(["facts", "graph", "summaries"], 10, progs, **kw).status == "ready"


def test_barrier_unconfigured_not_pending_and_never_claimed():
    clock = ManualClock()
    r = read_barrier(
        ["facts", "graph"], 5, {"facts": prog(5)}, timeout=timedelta(hours=1), clock=clock
    )
    assert r.status == "partial" and r.unavailable == ("graph",) and r.pending == ()
    assert "graph" not in r.ready and r.missing == ("graph",)
    ok = read_barrier(["facts"], 5, {"facts": prog(5)}, timeout=timedelta(0), clock=clock)
    assert ok.status == "ready"


def test_barrier_holes_reported_and_failed_missing():
    clock = ManualClock()
    r = read_barrier(
        ["facts", "graph"],
        8,
        {"facts": prog(8, holes=(2,)), "graph": prog(8)},
        timeout=timedelta(0),
        clock=clock,
        failed=frozenset({"graph"}),
    )
    assert r.status == "partial" and r.holes == {"facts": (2,)} and r.failed == ("graph",)
    assert r.ready == ("facts",) and r.missing == ("graph",)


def test_budget_target_and_defer_never_drops():
    b = BackgroundBudget(BudgetConfig(calls_per_turn=0.2, window_turns=10))
    assert b.decide(S, 1).action == "defer"  # no turns yet
    b.record_turns(S, 5)  # 1.0 call accrued
    d = b.decide(S, 1)
    assert d.action == "allow" and d.charged
    assert b.decide(S, 1, priority="optional").action == "skip_optional"
    assert b.decide(S, 1).action == "defer"
    st = b.stats(S)
    assert (st.deferred, st.skipped_optional, st.spent_calls, st.deferred_calls) == (2, 1, 1, 2)
    b.record_turns(S, 5)
    assert b.decide(S, 1).action == "allow"  # the deferred work gets done later


def test_budget_model_free_not_charged_and_scopes_independent():
    b = BackgroundBudget(BudgetConfig(calls_per_turn=0.2))
    d = b.decide(S, 0, 0)
    assert d.action == "allow" and not d.charged and b.stats(S).spent_calls == 0
    b.record_turns(S, 10)
    assert b.decide(S2, 1).action == "defer"
    assert b.decide(S, 1).action == "allow"


def test_budget_carry_over_capped_and_off():
    b = BackgroundBudget(BudgetConfig(calls_per_turn=0.5, window_turns=4, max_carry_windows=1.0))
    b.record_turns(S, 12)  # three idle windows; carry capped at one window (2.0)
    assert b.remaining(S)[0] == 2.0
    nb = BackgroundBudget(BudgetConfig(calls_per_turn=0.5, window_turns=4, carry_over=False))
    nb.record_turns(S, 12)
    assert nb.remaining(S)[0] == 0.0


def test_budget_tokens_unmetered_and_oversize():
    b = BackgroundBudget(BudgetConfig(calls_per_turn=None, tokens_per_turn=100, window_turns=2))
    b.record_turns(S, 1)
    assert b.decide(S, 1, 150).action == "defer"
    assert b.decide(S, 1, 100).action == "allow"
    assert BackgroundBudget(BudgetConfig(None, None)).decide(S, 99, 99999).action == "allow"
    o = BackgroundBudget(BudgetConfig(calls_per_turn=0.2, window_turns=10))  # window = 2 calls
    o.record_turns(S, 1)
    d = o.decide(S, 10)  # oversize accepted work is not starved
    assert d.action == "allow" and d.reason == "oversize_overdraft"
    assert o.decide(S, 1, priority="optional").action == "skip_optional"
    o.record_turns(S, 9)  # window ends in debt
    assert o.remaining(S)[0] < 0


def test_determinism():
    def run() -> list:
        b = BackgroundBudget(BudgetConfig(calls_per_turn=0.3, window_turns=3))
        out: list = []
        for i in range(20):
            b.record_turns(S, 1)
            out.append(b.decide(S, 1, priority="optional" if i % 3 else "accepted").action)
        return [*out, b.stats(S)]

    assert run() == run()
