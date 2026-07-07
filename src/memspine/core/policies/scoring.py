"""Scoring policy contract (M1): recency/relevance/importance + utility modifier.

Logic lands in Phase 1; Phase 0 fixes the options schema and signature.
"""

from __future__ import annotations

from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["ScoringPolicy"]


class ScoringOptions(PolicyOptions):
    recency_half_life_days: float = constants.SCORING_RECENCY_HALF_LIFE_DAYS
    importance_weight: float = constants.SCORING_IMPORTANCE_WEIGHT
    relevance_weight: float = constants.SCORING_RELEVANCE_WEIGHT
    utility_weight: float = constants.SCORING_UTILITY_WEIGHT


class ScoringPolicy(BindablePolicy):
    name: ClassVar[str] = "scoring"
    Options: ClassVar[type[PolicyOptions]] = ScoringOptions

    def composite_score(self, record: MemoryRecord) -> float:
        raise NotImplementedError("scoring logic lands in Phase 1 (plan §5)")
