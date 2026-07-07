"""Scoring policy math (M1): recency decay, weights, utility modifier."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine.core.policies.scoring import ScoringPolicy
from memspine.core.records import MemoryRecord, ScoringState

NOW = datetime(2026, 7, 7, tzinfo=UTC)


def rec(days_old: float = 0.0, importance: float = 0.0, utility: float = 0.0) -> MemoryRecord:
    ts = NOW - timedelta(days=days_old)
    return MemoryRecord(
        namespace="n",
        memory_type="semantic",
        content="x",
        recorded_at=ts,
        scoring=ScoringState(importance=importance, utility=utility),
    )


def test_recency_halves_at_half_life() -> None:
    policy = ScoringPolicy.bind(
        {"recency_half_life_days": 7, "relevance_weight": 0, "importance_weight": 0}
    )
    fresh = policy.composite_score(rec(days_old=0), now=NOW)
    aged = policy.composite_score(rec(days_old=7), now=NOW)
    assert abs(aged / fresh - 0.5) < 1e-9


def test_relevance_and_importance_weighting() -> None:
    policy = ScoringPolicy.bind({"recency_weight": 0, "utility_weight": 0})
    low = policy.composite_score(rec(importance=0.2), relevance=0.2, now=NOW)
    high = policy.composite_score(rec(importance=0.9), relevance=0.9, now=NOW)
    assert high > low
    assert abs(high - 0.9) < 1e-9  # weighted mean of equal signals


def test_utility_modifier_lifts_proven_memories() -> None:
    policy = ScoringPolicy.bind({"utility_weight": 0.5})
    plain = policy.composite_score(rec(), relevance=0.5, now=NOW)
    proven = policy.composite_score(rec(utility=1.0), relevance=0.5, now=NOW)
    assert abs((proven - plain) - 0.5) < 1e-9


def test_last_accessed_beats_recorded_at_as_recency_anchor() -> None:
    policy = ScoringPolicy.bind(
        {"relevance_weight": 0, "importance_weight": 0, "utility_weight": 0}
    )
    old_but_touched = rec(days_old=30)
    old_but_touched.scoring.last_accessed_at = NOW
    untouched = rec(days_old=30)
    assert policy.composite_score(old_but_touched, now=NOW) > policy.composite_score(
        untouched, now=NOW
    )
