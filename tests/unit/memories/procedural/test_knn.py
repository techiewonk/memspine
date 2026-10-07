"""W17f / N29 pure kNN vote and label table (ADR-060)."""

from __future__ import annotations

from memspine.memories.procedural.knn import (
    Exemplar,
    bm25_scores,
    knn_vote,
    label_table,
    render_table,
)

EXEMPLARS = [
    Exemplar("how do I reset my password", "howto"),
    Exemplar("how can I change my email address", "howto"),
    Exemplar("why was my card declined", "why"),
    Exemplar("why is my account locked", "why"),
    Exemplar("what does overdraft mean", "define"),
]


def test_bm25_prefers_matching_doc() -> None:
    scores = bm25_scores("password reset", [e.text for e in EXEMPLARS])
    assert scores[0] == max(scores) and scores[0] > 0
    assert bm25_scores("x", []) == []


def test_vote_picks_majority_label_with_margin() -> None:
    result = knn_vote("why was my payment declined", EXEMPLARS, k=5)
    assert result.label == "why"
    assert 0 < result.margin <= 1
    assert result.exemplars and result.exemplars[0][0].label == "why"


def test_dense_leg_is_fused() -> None:
    dense = [0.0, 0.0, 0.0, 0.0, 0.9]
    result = knn_vote("meaning of the word", EXEMPLARS, dense=dense, k=1)
    assert result.label == "define"


def test_no_match_gives_none() -> None:
    assert knn_vote("zzz qqq", EXEMPLARS).label is None
    assert knn_vote("anything", []).label is None


def test_label_table_distinctive_terms() -> None:
    table = label_table(EXEMPLARS, terms=3)
    assert set(table) == {"howto", "why", "define"}
    assert "overdraft" in table["define"]
    assert all(len(words) <= 3 for words in table.values())
    assert "label why ≈ {" in render_table(table)
