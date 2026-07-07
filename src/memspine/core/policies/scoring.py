"""Scoring policy contract (M1): recency/relevance/importance + utility modifier.

Logic lands in Phase 1; Phase 0 fixes the options schema and signature.
"""

from __future__ import annotations

from typing import ClassVar

from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord

__all__ = ["ScoringPolicy"]


class ScoringOptions(PolicyOptions):
    recency_half_life_days: float = 7.0
    importance_weight: float = 1.0
    relevance_weight: float = 1.0
    utility_weight: float = 0.5


class ScoringPolicy(BindablePolicy):
    name: ClassVar[str] = "scoring"
    Options: ClassVar[type[PolicyOptions]] = ScoringOptions

    def composite_score(self, record: MemoryRecord) -> float:
        raise NotImplementedError("scoring logic lands in Phase 1 (plan §5)")
