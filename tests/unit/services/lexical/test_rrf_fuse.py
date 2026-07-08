"""RRF fusion math + deterministic tie-breaks (D-25, implemented once in the port)."""

from __future__ import annotations

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


def test_ties_break_by_record_id_ascending() -> None:
    # a and b surface at identical rank 1 in the two legs → equal fused score.
    fused = rrf_fuse([VectorHit("b", 1.0)], [LexicalHit("a", 1.0)])
    assert [rid for rid, _ in fused] == ["a", "b"]  # deterministic: id asc
    assert fused[0][1] == fused[1][1]


def test_empty_legs_fuse_to_empty() -> None:
    assert rrf_fuse([], []) == []


def test_single_leg_preserves_that_legs_order() -> None:
    lexical = [LexicalHit("x", 9.0), LexicalHit("y", 3.0), LexicalHit("z", 1.0)]
    assert [rid for rid, _ in rrf_fuse([], lexical)] == ["x", "y", "z"]
