"""#54: ``Engine.feedback`` (like / dislike / note) as FEEDBACK events feeding utility."""

from __future__ import annotations

import math
from typing import Any

import pytest

from memspine.config import constants
from memspine.core.policies.scoring import ScoringPolicy, feedback_utility
from memspine.store.records import MemoryRecord, ScoringState

_BASE: dict[str, Any] = {
    "model": {"embedding": {"provider": "hash"}},
    "read": {"hybrid": False},
}


def test_feedback_term_is_bounded_and_zero_without_feedback() -> None:
    assert feedback_utility(ScoringState()) == 0.0
    assert feedback_utility(ScoringState(likes=3)) == pytest.approx(
        math.tanh(3 / constants.FEEDBACK_UTILITY_SCALE)
    )
    assert 0.0 < feedback_utility(ScoringState(likes=10_000)) <= 1.0
    assert feedback_utility(ScoringState(dislikes=2)) < 0.0


def _record(**scoring: int) -> MemoryRecord:
    return MemoryRecord(
        namespace="a", memory_type="semantic", content="x", scoring=ScoringState(**scoring)
    )


@pytest.mark.parametrize("mode", ["blend", "relevance_first"])
def test_utility_weight_gates_the_feedback_term(mode: str) -> None:
    on = ScoringPolicy.bind({"utility_weight": 0.5, "mode": mode})
    off = ScoringPolicy.bind({"utility_weight": 0.0, "mode": mode})
    liked, plain, disliked = _record(likes=4), _record(), _record(dislikes=4)
    now = plain.recorded_at
    score_on = [on.composite_score(r, 0.5, now) for r in (liked, plain, disliked)]
    assert score_on[0] > score_on[1] > score_on[2]
    score_off = [off.composite_score(r, 0.5, now) for r in (liked, plain, disliked)]
    assert score_off[0] == pytest.approx(score_off[1]) == pytest.approx(score_off[2])
