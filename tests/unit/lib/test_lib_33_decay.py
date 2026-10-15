# ruff: noqa: E501
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from memspine.derive.decay import (
    ColdGroup,
    CompressBudget,
    DecayPolicy,
    GroupMember,
    RecordStats,
    Resolution,
    Tier,
    activation,
    compression_plan,
    tier_plan,
)

NOW = datetime(2026, 10, 13, 12, 0, tzinfo=UTC)


def rec(rid: str = "r1", idle: float = 0.0, **kw) -> RecordStats:
    return RecordStats(record_id=rid, recorded_at=NOW - timedelta(days=idle), **kw)


def mem(rid: str, tokens: int = 100, **kw) -> GroupMember:
    return GroupMember(record_id=rid, tokens=tokens, **kw)


# ---- activation maths -------------------------------------------------------------------------------


def test_activation_formula_exact():
    pol = DecayPolicy()
    r = rec(idle=30.0, kind="episodic", access_count=0, importance=0.5)
    expected = (0.6 * math.exp(-1.0) + 0.0 + 0.15 * 0.5) / 1.0
    assert activation(r, NOW, pol) == pytest.approx(expected)


def test_activation_recency_monotone_decreasing():
    vals = [activation(rec(idle=d), NOW) for d in (0, 1, 7, 30, 90, 365)]
    assert vals == sorted(vals, reverse=True)
    assert vals[0] > vals[-1]


def test_activation_strength_per_kind():
    slow = activation(rec(idle=60, kind="procedural"), NOW)
    fast = activation(rec(idle=60, kind="working"), NOW)
    assert slow > fast


def test_activation_frequency_and_importance_help_and_saturate():
    base = activation(rec(idle=30), NOW)
    assert activation(rec(idle=30, access_count=5), NOW) > base
    assert activation(rec(idle=30, importance=0.9), NOW) > base
    sat = activation(rec(idle=30, access_count=20), NOW)
    assert activation(rec(idle=30, access_count=2000), NOW) == pytest.approx(sat)


def test_activation_bounded_unit_interval():
    top = activation(rec(idle=0, access_count=10**6, importance=1.0), NOW)
    assert 0.0 <= top <= 1.0 + 1e-12
    assert top == pytest.approx(1.0)
    assert 0.0 <= activation(rec(idle=10**5, importance=0.0), NOW) < 0.01


def test_access_restores_recency():
    stale = rec(idle=100)
    touched = rec(idle=100, last_accessed_at=NOW - timedelta(hours=1))
    assert activation(touched, NOW) > activation(stale, NOW)


def test_future_timestamps_and_naive_datetimes_are_safe():
    future = RecordStats(record_id="f", recorded_at=NOW + timedelta(days=3))
    assert activation(future, NOW) == pytest.approx(activation(rec(idle=0), NOW))
    naive = RecordStats(record_id="n", recorded_at=datetime(2026, 10, 3, 12, 0))
    assert activation(naive, NOW) == pytest.approx(activation(rec(idle=10), NOW))


def test_policy_validation():
    with pytest.raises(ValidationError):
        DecayPolicy(boundaries=(0.3, 0.5, 0.1))
    with pytest.raises(ValidationError):
        DecayPolicy(margin=0.3)
    with pytest.raises(ValidationError):
        DecayPolicy(w_rec=0, w_freq=0, w_imp=0)
    with pytest.raises(ValidationError):
        DecayPolicy(strength_days={"episodic": 0})


# ---- tier plan ---------------------------------------------------------------------------------------


def test_fresh_record_stays_hot_and_idle_record_cools():
    assert tier_plan([rec(idle=0)], NOW) == []
    (move,) = tier_plan([rec(idle=400)], NOW)
    assert (move.from_tier, move.to_tier, move.reason) == (Tier.HOT, Tier.DORMANT, "idle")


def test_default_boundaries_roughly_match_o22_days():
    def tier_at(days: float) -> Tier:
        moves = tier_plan([rec(idle=days)], NOW)
        return moves[0].to_tier if moves else Tier.HOT

    assert tier_at(3) is Tier.HOT
    assert tier_at(15) is Tier.WARM
    assert tier_at(40) is Tier.COLD
    assert tier_at(120) is Tier.DORMANT


def test_reactivation_promotes_back_to_hot():
    r = rec(
        idle=200, tier=Tier.DORMANT, last_accessed_at=NOW - timedelta(minutes=5), access_count=3
    )
    (move,) = tier_plan([r], NOW)
    assert (move.to_tier, move.reason) == (Tier.HOT, "reactivated")


def test_hysteresis_dead_band_prevents_flapping():
    pol = DecayPolicy()
    b = pol.boundaries[0]  # hot|warm
    # find an idle time whose activation lies strictly inside [b, b + margin)
    lo, hi = 0.0, 400.0
    for _ in range(80):
        mid = (lo + hi) / 2
        if activation(rec(idle=mid), NOW, pol) > b + pol.margin / 2:
            lo = mid
        else:
            hi = mid
    inside = rec(idle=lo)
    assert b <= activation(inside, NOW, pol) < b + pol.margin
    # In the band: hot stays hot, warm stays warm. Same activation, no move either way.
    assert tier_plan([inside.model_copy(update={"tier": Tier.HOT})], NOW, pol) == []
    assert tier_plan([inside.model_copy(update={"tier": Tier.WARM})], NOW, pol) == []


def test_no_flapping_without_access():
    # daily sweeps with accesses on a few days: a promotion may only happen on an access day, and between
    # accesses the tier only ever gets colder
    pol = DecayPolicy()
    tier = Tier.HOT
    access_days = {0, 37, 90}
    last: datetime | None = None
    start = NOW - timedelta(days=200)
    for day in range(200):
        sim_now = start + timedelta(days=day)
        accessed = day in access_days
        if accessed:
            last = sim_now
        rec_ = RecordStats(record_id="x", recorded_at=start, last_accessed_at=last, tier=tier)
        moves = tier_plan([rec_], sim_now, pol)
        if moves:
            if moves[0].to_tier < tier:
                assert accessed
            tier = moves[0].to_tier


def test_idempotent_after_applying_moves():
    records = [rec(f"r{i}", idle=i * 13) for i in range(12)]
    moves = {m.record_id: m.to_tier for m in tier_plan(records, NOW)}
    applied = [r.model_copy(update={"tier": moves.get(r.record_id, r.tier)}) for r in records]
    assert tier_plan(applied, NOW) == []


@pytest.mark.parametrize("flag", ["pinned", "standing", "contested"])
def test_protected_records_never_demoted(flag):
    r = rec(idle=1000, importance=0.0, **{flag: True})
    assert tier_plan([r], NOW) == []


def test_protected_kind_never_demoted_and_cold_protected_restored():
    pol = DecayPolicy(protected_kinds=frozenset({"standing_instruction"}))
    r = rec(idle=1000, kind="standing_instruction")
    assert tier_plan([r], NOW, pol) == []
    cold = rec(idle=1000, pinned=True, tier=Tier.COLD)
    (move,) = tier_plan([cold], NOW)
    assert (move.to_tier, move.reason) == (Tier.HOT, "protected_restore")


def test_legal_hold_quarantined_deleted_are_untouched():
    for flag in ("legal_hold", "quarantined", "deleted"):
        assert tier_plan([rec(idle=1000, **{flag: True})], NOW) == []


def test_plans_deterministic_and_order_independent():
    records = [rec(f"r{i:02d}", idle=(i * 17) % 300, access_count=i % 4) for i in range(30)]
    a = tier_plan(records, NOW)
    b = tier_plan(list(reversed(records)), NOW)
    assert a == b
    assert [m.record_id for m in a] == sorted(m.record_id for m in a)
    assert tier_plan(iter(records), NOW) == a


def test_l0_never_deleted_only_tiered():
    raw = [rec(f"l0_{i}", idle=5000, layer="L0") for i in range(3)]
    moves = tier_plan(raw, NOW)
    assert moves and all(isinstance(m.to_tier, Tier) for m in moves)
    assert not any(hasattr(m, "delete") or "delete" in m.model_dump() for m in moves)


# ---- compression plan --------------------------------------------------------------------------------


def group(gid: str, *members: GroupMember) -> ColdGroup:
    return ColdGroup(group_id=gid, members=tuple(members))


def test_job_levels_lineage_and_budgets():
    g = group("g1", mem("b", 300), mem("a", 400), mem("c", 300))
    (job,) = compression_plan([g])
    assert job.lineage == ("a", "b", "c")
    assert job.input_tokens == 1000
    assert [(s.level, s.source_level, s.token_budget) for s in job.levels] == [
        (Resolution.PARAGRAPH, Resolution.FULL, 200),
        (Resolution.SENTENCE, Resolution.PARAGRAPH, 40),
        (Resolution.KEYWORD, Resolution.SENTENCE, 12),
    ]
    assert job.member_effect == "retain"
    assert job.member_tier_after is Tier.DORMANT


def test_small_group_skips_levels_that_do_not_shrink():
    g = group("g", mem("a", 30), mem("b", 30))  # 60 tokens: paragraph (200) is pointless
    (job,) = compression_plan([g])
    assert [s.level for s in job.levels] == [Resolution.SENTENCE, Resolution.KEYWORD]
    assert job.levels[0].source_level is Resolution.FULL
    tiny = group("t", mem("a", 10), mem("b", 10))
    assert [s.level for s in compression_plan([tiny])[0].levels] == [Resolution.KEYWORD]
    assert compression_plan([group("z", mem("a", 3), mem("b", 4))]) == []


def test_members_are_never_dropped_from_lineage_by_budget():
    g = group("g", *[mem(f"m{i}", 100) for i in range(5)])
    (job,) = compression_plan([g], CompressBudget(paragraph_tokens=50))
    assert len(job.lineage) == 5


def test_protected_and_held_members_excluded():
    g = group(
        "g",
        mem("a"),
        mem("b"),
        mem("pin", pinned=True),
        mem("st", standing=True),
        mem("ct", contested=True),
        mem("hold", legal_hold=True),
        mem("q", quarantined=True),
        mem("warm", tier=Tier.WARM),
    )
    (job,) = compression_plan([g])
    assert job.lineage == ("a", "b")


def test_group_below_min_size_or_already_compressed_skipped():
    assert compression_plan([group("g", mem("a"), mem("pin", pinned=True))]) == []
    assert (
        compression_plan([group("g", mem("a", compressed=True), mem("b", compressed=True))]) == []
    )
    assert compression_plan([group("g", mem("a", compressed=True), mem("b"))]) != []


def test_caps_prefer_largest_groups_and_are_deterministic():
    gs = [
        group("s", mem("a", 50), mem("b", 50)),
        group("m", mem("c", 300), mem("d", 300)),
        group("l", mem("e", 900), mem("f", 900)),
    ]
    jobs = compression_plan(gs, CompressBudget(max_jobs=2))
    assert [j.group_id for j in jobs] == ["l", "m"]
    jobs = compression_plan(gs, CompressBudget(max_input_tokens=800))
    assert [j.group_id for j in jobs] == ["m", "s"]
    assert compression_plan(gs) == compression_plan(list(reversed(gs)))
    assert compression_plan(gs, CompressBudget(max_jobs=0)) == []


def test_job_id_stable_and_sensitive_to_members():
    g = group("g", mem("a"), mem("b"))
    assert compression_plan([g])[0].job_id == compression_plan([g])[0].job_id
    g2 = group("g", mem("a"), mem("c"))
    assert compression_plan([g2])[0].job_id != compression_plan([g])[0].job_id


def test_compression_never_deletes_members_and_l0_only_retained():
    g = group("g", mem("raw1", layer="L0"), mem("raw2", layer="L0"), mem("f", layer="L1"))
    (job,) = compression_plan([g])
    assert job.member_effect == "retain"
    assert set(job.lineage) == {"raw1", "raw2", "f"}
    fields = set(type(job).model_fields)
    assert not any("delete" in f or "drop" in f or "remove" in f for f in fields)
    with pytest.raises(ValidationError):
        type(job)(**{**job.model_dump(), "member_effect": "delete"})


def test_member_tier_after_is_configurable_and_inputs_are_immutable():
    (job,) = compression_plan(
        [group("g", mem("a"), mem("b"))], CompressBudget(member_tier_after=Tier.COLD)
    )
    assert job.member_tier_after is Tier.COLD
    with pytest.raises(ValidationError):
        job.group_id = "x"  # frozen
