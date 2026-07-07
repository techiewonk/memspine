"""Decay policy (M3): tier boundaries + reinforcement anchor."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.policies.decay import DecayPolicy, DecayTier
from memspine.core.records import MemoryRecord, ScoringState

NOW = datetime(2026, 7, 7, tzinfo=UTC)


def rec(idle_days: float, accessed_days_ago: float | None = None) -> MemoryRecord:
    scoring = ScoringState()
    if accessed_days_ago is not None:
        scoring = ScoringState(last_accessed_at=NOW - timedelta(days=accessed_days_ago))
    return MemoryRecord(
        namespace="agent/a",
        memory_type="episodic",
        content="x",
        recorded_at=NOW - timedelta(days=idle_days),
        scoring=scoring,
    )


def test_tier_boundaries_are_absolute() -> None:
    policy = DecayPolicy.bind()
    assert policy.tier_for(rec(0), NOW) is DecayTier.HOT
    assert policy.tier_for(rec(6.9), NOW) is DecayTier.HOT
    assert policy.tier_for(rec(7), NOW) is DecayTier.WARM
    assert policy.tier_for(rec(29.9), NOW) is DecayTier.WARM
    assert policy.tier_for(rec(30), NOW) is DecayTier.COLD
    assert policy.tier_for(rec(89.9), NOW) is DecayTier.COLD
    assert policy.tier_for(rec(90), NOW) is DecayTier.DORMANT
    assert policy.tier_for(rec(500), NOW) is DecayTier.DORMANT


def test_recent_access_restores_hot_in_one_step() -> None:
    """Reinforcement (M1/M3): a fresh retrieval outweighs an old recorded_at."""
    policy = DecayPolicy.bind()
    dormant_but_just_read = rec(idle_days=200, accessed_days_ago=1)
    assert policy.tier_for(dormant_but_just_read, NOW) is DecayTier.HOT


def test_recorded_at_newer_than_access_wins() -> None:
    """An edit after the last retrieval counts as the most recent touch."""
    policy = DecayPolicy.bind()
    edited_after_read = rec(idle_days=2, accessed_days_ago=50)
    assert policy.tier_for(edited_after_read, NOW) is DecayTier.HOT


def test_custom_thresholds_bind_from_config() -> None:
    policy = DecayPolicy.bind(
        {"hot_to_warm_days": 1, "warm_to_cold_days": 2, "cold_to_dormant_days": 3}
    )
    assert policy.tier_for(rec(1.5), NOW) is DecayTier.WARM
    assert policy.tier_for(rec(2.5), NOW) is DecayTier.COLD
    assert policy.tier_for(rec(3.5), NOW) is DecayTier.DORMANT
