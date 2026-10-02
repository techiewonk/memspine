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
