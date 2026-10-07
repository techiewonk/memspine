"""H3: aggregation detection and core terms."""

from __future__ import annotations

import pytest

from memspine.core.query_shape import core_terms, is_aggregation


@pytest.mark.parametrize(
    "q",
    [
        "How many times has Melanie gone to the beach in 2023?",
        "What books has Melanie read?",
        "What are Melanie's pets' names?",
        "List the places Caroline visited.",
        "What activities does Melanie do with her kids?",
        "How often does John go running?",
    ],
)
def test_aggregation_questions(q: str) -> None:
    assert is_aggregation(q)


@pytest.mark.parametrize(
    "q",
    [
        "When did Caroline go to the LGBTQ support group?",
        "Where did Melanie move from?",
        "What is Caroline's identity?",
    ],
)
def test_single_fact_questions(q: str) -> None:
    assert not is_aggregation(q)


def test_core_terms_drops_function_words() -> None:
    assert core_terms("How many times has Melanie gone to the beach in 2023?") == (
        "times Melanie gone beach 2023"
    )


@pytest.mark.parametrize(
    "q",
    [
        "When did Caroline first go hiking?",
        "What is the latest book Melanie read?",
        "What did John do most recently?",
    ],
)
def test_ordering_questions(q: str) -> None:
    from memspine.core.query_shape import is_ordering

    assert is_ordering(q)


def test_non_ordering_question() -> None:
    from memspine.core.query_shape import is_ordering

    assert not is_ordering("Where does Caroline live?")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("When did Caroline give a speech?", True),
        ("How many weeks after the move did she adopt?", True),
        ("Which year did they meet?", True),
        ("What activities does Melanie do?", False),
        ("whenever possible, call me", False),
    ],
)
def test_is_temporal(query: str, expected: bool) -> None:
    from memspine.core.query_shape import is_temporal

    assert is_temporal(query) is expected


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("How many times has Melanie gone to the beach in 2023?", "compose"),
        ("What books has Melanie read?", "compose"),
        ("How many months passed between the two trips?", None),
        ("What was the first concert Caroline went to?", "replay"),
        ("When did Caroline go to the LGBTQ support group?", None),
        ("Why did Jon open a dance studio?", None),
        ("Where did Melanie move from?", None),
    ],
)
def test_rule_read_mode(q: str, expected: str | None) -> None:
    """G24: the rules the decision planner applies before asking its provider."""
    from memspine.core.query_shape import rule_read_mode

    assert rule_read_mode(q) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What does Gina say about the dancers in the photo?", True),
        ("What did Jon say about Gina's progress with her store?", True),
        ("How do Melanie and Caroline describe their journey together?", True),
        ("What did the posters at the poetry reading say?", True),
        ("What exactly did my landlord tell me about the deposit?", True),
        ("Can you give me her exact words?", True),
        ("What does Evan mention about his progress at the gym?", True),
        ("Which movie does Tim mention they enjoy watching?", False),
        ("Is the friend who wrote Deborah the motivational quote alive?", False),
        ("What did Gina design for her store?", False),
        ("When did Caroline say she would move?", False),
    ],
)
def test_is_verbatim(query: str, expected: bool) -> None:
    """F5 (plan v3.2): questions that ask for someone's own words."""
    from memspine.core.query_shape import is_verbatim

    assert is_verbatim(query) is expected


def test_feedback_terms_are_shared_words_the_query_lacks() -> None:
    """N03 (plan v3.2): pseudo-relevance feedback terms."""
    from memspine.core.query_shape import feedback_terms

    texts = [
        "Melanie: we went camping at the lake with the tent",
        "Melanie: the tent leaked at the lake again",
        "Caroline: painting by the lake is calming",
    ]
    assert feedback_terms("Where did Melanie go camping?", texts) == "lake tent"
    assert feedback_terms("lake tent", texts) == ""
