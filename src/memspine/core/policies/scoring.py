"""Scoring policy (M1): recency/relevance/importance + utility modifier.

``composite_score`` is pure math over a record plus a query-relevance signal —
no I/O, deterministic, so retrieval ranking is unit-testable and reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["ScoringPolicy"]


class ScoringOptions(PolicyOptions):
    recency_half_life_days: float = constants.SCORING_RECENCY_HALF_LIFE_DAYS
    recency_weight: float = 1.0
    importance_weight: float = constants.SCORING_IMPORTANCE_WEIGHT
    relevance_weight: float = constants.SCORING_RELEVANCE_WEIGHT
    utility_weight: float = constants.SCORING_UTILITY_WEIGHT


class ScoringPolicy(BindablePolicy):
    name: ClassVar[str] = "scoring"
    Options: ClassVar[type[PolicyOptions]] = ScoringOptions

    def composite_score(
        self,
        record: MemoryRecord,
        relevance: float = 0.0,
        now: datetime | None = None,
    ) -> float:
        """Weighted mean of recency/relevance/importance, shifted by utility.

        The three base signals are each in [0, 1]; the utility modifier
        (reinforcement signal, M1) adds ``utility_weight * utility`` on top so
        proven-useful memories can outrank fresher ones.
        """
        options = self.options
        assert isinstance(options, ScoringOptions)
        now = now or datetime.now(UTC)

        anchor = record.scoring.last_accessed_at or record.recorded_at
        age_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
        recency = 0.5 ** (age_days / options.recency_half_life_days)

        weight_sum = options.recency_weight + options.relevance_weight + options.importance_weight
        if weight_sum <= 0.0:
            base = 0.0
        else:
            base = (
                options.recency_weight * recency
                + options.relevance_weight * relevance
                + options.importance_weight * record.scoring.importance
            ) / weight_sum
        return base + options.utility_weight * record.scoring.utility
