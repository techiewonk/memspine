"""RRF fusion math + deterministic tie-breaks (D-25, implemented once in the port)."""

from __future__ import annotations

import math
import random

import pytest

from memspine.config.constants import RRF_K
from memspine.services.lexical.base import LexicalHit, rrf_fuse
from memspine.services.vector.base import VectorHit


def test_records_in_both_legs_outrank_single_leg_records() -> None:
    vector = [VectorHit("a", 0.9), VectorHit("b", 0.8)]
    lexical = [LexicalHit("b", 5.0), LexicalHit("c", 4.0)]
    fused = rrf_fuse(vector, lexical)
    # b appears in both legs (rank 2 vector + rank 1 lexical) → highest fused score.
    assert [rid for rid, _ in fused] == ["b", "a", "c"]
    scores = dict(fused)
    assert scores["b"] == 1.0 / (RRF_K + 2) + 1.0 / (RRF_K + 1)
    assert scores["a"] == 1.0 / (RRF_K + 1)
    assert scores["c"] == 1.0 / (RRF_K + 2)


def test_ties_break_by_leg_ranks_not_record_id() -> None:
    # a and b surface at identical rank 1 in the two legs → equal fused score.
    # #87: the vector leg's hit comes first whatever the ids (they are random
    # uuids, so an id tie-break reordered equal records from run to run).
    fused = rrf_fuse([VectorHit("b", 1.0)], [LexicalHit("a", 1.0)])
    assert [rid for rid, _ in fused] == ["b", "a"]
    assert fused[0][1] == fused[1][1]
    swapped = rrf_fuse([VectorHit("a", 1.0)], [LexicalHit("b", 1.0)])
    assert [rid for rid, _ in swapped] == ["a", "b"]


def test_empty_legs_fuse_to_empty() -> None:
    assert rrf_fuse([], []) == []


def test_single_leg_preserves_that_legs_order() -> None:
    lexical = [LexicalHit("x", 9.0), LexicalHit("y", 3.0), LexicalHit("z", 1.0)]
    assert [rid for rid, _ in rrf_fuse([], lexical)] == ["x", "y", "z"]


def _reference_rrf(
    vector: list[VectorHit],
    lexical: list[LexicalHit],
    *,
    k: int = RRF_K,
    extra: tuple[list[LexicalHit], ...] = (),
    weights: list[float] | None = None,
) -> list[tuple[str, float]]:
    legs = (vector, lexical, *extra)
    fused: dict[str, float] = {}
    ranks: dict[str, list[int]] = {}
    absent = 1 + max((len(hits) for hits in legs), default=0)
    for leg, hits in enumerate(legs):
        for rank, hit in enumerate(hits, start=1):
            rid = hit.record_id
            slots = ranks.setdefault(rid, [absent] * len(legs))
            slots[leg] = min(slots[leg], rank)
            weight = 1.0 if weights is None or leg >= len(weights) else weights[leg]
            fused[rid] = fused.get(rid, 0.0) + weight / (k + rank)
    return sorted(fused.items(), key=lambda item: (-item[1], ranks[item[0]]))


def _assert_same_order_and_scores(
    expected: list[tuple[str, float]], actual: list[tuple[str, float]]
) -> None:
    assert [rid for rid, _ in actual] == [rid for rid, _ in expected]
    for (_, expected_score), (_, actual_score) in zip(expected, actual, strict=True):
        if math.isnan(expected_score):
            assert math.isnan(actual_score)
        else:
            assert actual_score == expected_score


def test_limit_is_exact_prefix_for_seeded_adversarial_legs() -> None:
    rng = random.Random(92017)
    for case in range(80):
        vector = [
            VectorHit(f"r{rng.randrange(23)}", rng.choice([0.0, 0.5, 1.0]))
            for _ in range(case % 29)
        ]
        lexical = [
            LexicalHit(f"r{rng.randrange(23)}", rng.choice([-2.0, 0.0, 3.0]))
            for _ in range((case * 7) % 31)
        ]
        extra = ([LexicalHit(f"r{rng.randrange(23)}", 1.0) for _ in range((case * 11) % 19)],)
        weights = [rng.choice([-3.0, 0.0, 0.5, 2.0]), rng.choice([-1.0, 1.0, 4.0]), 0.25]
        expected = _reference_rrf(vector, lexical, k=7, extra=extra, weights=weights)
        for limit in (0, 1, 3, 10, len(expected), len(expected) + 2):
            actual = rrf_fuse(vector, lexical, k=7, extra=extra, weights=weights, limit=limit)
            _assert_same_order_and_scores(expected[:limit], actual)


def test_limit_preserves_nan_sort_behavior_and_signed_scores() -> None:
    vector = [VectorHit("neg-zero", 0.0), VectorHit("negative", 0.0), VectorHit("nan-a", 0.0)]
    lexical = [LexicalHit("nan-b", 1.0), LexicalHit("negative", 1.0)]
    expected = _reference_rrf(vector, lexical, weights=[-0.0, math.nan])
    actual = rrf_fuse(vector, lexical, weights=[-0.0, math.nan], limit=3)
    _assert_same_order_and_scores(expected[:3], actual)

    inf_expected = _reference_rrf(
        [VectorHit("positive-inf", 1.0)],
        [LexicalHit("negative-inf", 1.0)],
        weights=[math.inf, -math.inf],
    )
    inf_actual = rrf_fuse(
        [VectorHit("positive-inf", 1.0)],
        [LexicalHit("negative-inf", 1.0)],
        weights=[math.inf, -math.inf],
        limit=1,
    )
    _assert_same_order_and_scores(inf_expected[:1], inf_actual)

    large_vector = [VectorHit(f"nan-{index}", 1.0) for index in range(1100)]
    large_lexical = [LexicalHit(f"finite-{index}", 1.0) for index in range(20)]
    large_expected = _reference_rrf(large_vector, large_lexical, weights=[math.nan, 1.0])
    large_actual = rrf_fuse(large_vector, large_lexical, weights=[math.nan, 1.0], limit=3)
    _assert_same_order_and_scores(large_expected[:3], large_actual)


def test_zero_limit_keeps_score_accumulation_errors() -> None:
    with pytest.raises(ZeroDivisionError):
        rrf_fuse([VectorHit("a", 1.0)], [], k=-1, limit=0)


@pytest.mark.parametrize("limit", [-1, True, 1.5, "2"])
def test_limit_rejects_values_outside_nonnegative_integer_contract(limit: object) -> None:
    with pytest.raises(ValueError, match="limit must be a non-negative integer or None"):
        rrf_fuse([], [], limit=limit)  # type: ignore[arg-type]
