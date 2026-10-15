"""G-C7: official LoCoMo F1 port. Expected values were cross-checked against the upstream
snap-research/locomo task_eval/evaluation.py (eval_question_answering) on 2026-10-11, including a 1,748-case
differential run over a saved run's answers with zero mismatches."""

import pytest

pytest.importorskip("nltk")

from memspine_evals.locomo_f1 import (
    category_of,
    f1_multi,
    f1_score,
    locomo_f1,
    normalize_answer,
    row_f1,
    summarise,
)


def test_normalize_drops_commas_punct_articles_and_and():
    assert normalize_answer("The Cat, and a Dog!") == "cat dog"
    assert normalize_answer("  Hello,   World. ") == "hello world"


def test_f1_stems_and_is_a_bag_of_words_f1():
    assert f1_score("running dogs", "run dog") == 1.0
    assert f1_score("Melanie painted the sunrise in 2022.", "2022") == pytest.approx(1 / 3)
    assert f1_score("", "2022") == 0.0
    assert f1_score("cat", "dog") == 0.0


def test_category1_splits_on_commas_best_match_per_gold_part():
    # gold parts: rome, paris, berlin -> best matches 1, 1, 0
    assert f1_multi("Paris, Rome", "Rome, Paris, Berlin") == pytest.approx(2 / 3)
    assert locomo_f1("Paris, Rome", "Rome, Paris, Berlin", 1) == pytest.approx(2 / 3)


def test_category3_uses_first_part_of_gold_before_semicolon():
    assert locomo_f1("yes", "Yes; she likes it a lot", 3) == 1.0
    assert locomo_f1("yes", "Yes; she likes it a lot", 4) < 1.0  # no cut outside category 3


def test_category5_abstention_markers():
    assert (
        locomo_f1("Not mentioned in the conversation", "Not mentioned in the conversation", 5)
        == 1.0
    )
    assert locomo_f1("No information available.", "x", 5) == 1.0
    assert locomo_f1("She went to Paris", "x", 5) == 0.0


def test_gold_is_stringified_and_unknown_category_rejected():
    assert locomo_f1("2022", 2022, 2) == 1.0
    with pytest.raises(ValueError):
        locomo_f1("a", "a", 9)


def test_row_f1_handles_label_status_and_empty_answers():
    ok = {"type_label": "cat2", "gold": "2022", "answer": "It was 2022", "status": "completed"}
    assert row_f1(ok) == pytest.approx(0.5)
    assert row_f1({**ok, "status": "error"}) == 0.0
    assert row_f1({**ok, "answer": None}) == 0.0
    assert row_f1({**ok, "type_label": "other"}) is None
    assert category_of("cat4") == 4 and category_of("dev") is None


def test_summarise_headline_excludes_category5():
    s = summarise({1: [1.0, 0.0], 5: [1.0]})
    assert s["f1_1_4"] == 50.0 and s["n_1_4"] == 2
    assert s["by_category"][5]["f1"] == 100.0
