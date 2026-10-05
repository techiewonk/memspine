"""Scoring policy (M1): recency/relevance/importance + utility modifier.

``composite_score`` is pure math over a record plus a query-relevance signal —
no I/O, deterministic, so retrieval ranking is unit-testable and reproducible.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import ClassVar

from memspine.config import constants
from memspine.core.policies.base import BindablePolicy, PolicyOptions
from memspine.core.records import MemoryRecord, ScoringState

__all__ = ["ScoringPolicy", "feedback_utility", "utility_of"]


def feedback_utility(scoring: ScoringState) -> float:
    """#54: the bounded feedback term, ``tanh((likes - dislikes) / scale)`` in (-1, 1).

    Zero with no feedback, so records nobody rated score exactly as before. Each
    further like moves the term less (a spammed like count cannot grow it past 1)."""
    net = scoring.likes - scoring.dislikes
    if net == 0:
        return 0.0
    return math.tanh(net / constants.FEEDBACK_UTILITY_SCALE)


def utility_of(record: MemoryRecord) -> float:
    """The utility signal ``utility_weight`` multiplies: reinforcement-on-read (M1/A5)
    plus the user-feedback term (#54)."""
    return record.scoring.utility + feedback_utility(record.scoring)


class ScoringOptions(PolicyOptions):
    recency_half_life_days: float = constants.SCORING_RECENCY_HALF_LIFE_DAYS
    recency_weight: float = 1.0
    importance_weight: float = constants.SCORING_IMPORTANCE_WEIGHT
    relevance_weight: float = constants.SCORING_RELEVANCE_WEIGHT
    utility_weight: float = constants.SCORING_UTILITY_WEIGHT
    #: ``blend`` (default): the weighted mean below. ``relevance_first``: rank by
    #: relevance, and let recency/importance/utility only break near-ties (they
    #: contribute at most ``tie_break_weight``). On a static corpus, where event
    #: or ingest times cluster, the blend demotes the best match: on LoCoMo it
    #: cost about two-thirds of R@1 (0.30 relevance-only vs 0.08 blended).
    mode: str = "blend"
    tie_break_weight: float = 0.05


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
        proven-useful memories can outrank fresher ones. Utility includes the
        bounded user-feedback term (#54, :func:`feedback_utility`), zero for a record
        with no feedback.
        """
        options = self.options
        assert isinstance(options, ScoringOptions)
        now = now or datetime.now(UTC)

        anchor = record.scoring.last_accessed_at or record.recorded_at
        age_days = max(0.0, (now - anchor).total_seconds() / 86400.0)
        recency = 0.5 ** (age_days / options.recency_half_life_days)

        if options.mode == "relevance_first":
            other_sum = options.recency_weight + options.importance_weight
            other = (
                (
                    options.recency_weight * recency
                    + options.importance_weight * record.scoring.importance
                )
                / other_sum
                if other_sum > 0.0
                else 0.0
            )
            nudge = (other + options.utility_weight * utility_of(record)) / (
                1.0 + options.utility_weight
            )
            return relevance + options.tie_break_weight * nudge
        weight_sum = options.recency_weight + options.relevance_weight + options.importance_weight
        if weight_sum <= 0.0:
            base = 0.0
        else:
            base = (
                options.recency_weight * recency
                + options.relevance_weight * relevance
                + options.importance_weight * record.scoring.importance
            ) / weight_sum
        return base + options.utility_weight * utility_of(record)
