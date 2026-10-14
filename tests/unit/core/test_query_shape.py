"""H3: aggregation detection and core terms."""

from __future__ import annotations

import pytest

from memspine.models.encoders.query.shape import core_terms, is_aggregation


@pytest.mark.parametrize(
    "q",
    [
        "How many times has Bram gone to the beach in 2023?",
        "What books has Bram read?",
        "What are Bram's pets' names?",
        "List the places Ilse visited.",
        "What activities does Bram do with her kids?",
        "How often does Tomasz go running?",
    ],
)
def test_aggregation_questions(q: str) -> None:
    assert is_aggregation(q)


@pytest.mark.parametrize(
    "q",
    [
        "When did Ilse go to the LGBTQ support group?",
        "Where did Bram move from?",
        "What is Ilse's identity?",
    ],
)
def test_single_fact_questions(q: str) -> None:
    assert not is_aggregation(q)


def test_core_terms_drops_function_words() -> None:
    assert core_terms("How many times has Bram gone to the beach in 2023?") == (
        "times Bram gone beach 2023"
    )


@pytest.mark.parametrize(
    "q",
    [
        "When did Ilse first go hiking?",
        "What is the latest book Bram read?",
        "What did Tomasz do most recently?",
    ],
)
def test_ordering_questions(q: str) -> None:
    from memspine.models.encoders.query.shape import is_ordering

    assert is_ordering(q)


def test_non_ordering_question() -> None:
    from memspine.models.encoders.query.shape import is_ordering

    assert not is_ordering("Where does Ilse live?")


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("When did Ilse give a speech?", True),
        ("How many weeks after the move did she adopt?", True),
        ("Which year did they meet?", True),
        ("What activities does Bram do?", False),
        ("whenever possible, call me", False),
    ],
)
def test_is_temporal(query: str, expected: bool) -> None:
    from memspine.models.encoders.query.shape import is_temporal

    assert is_temporal(query) is expected


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("How many times has Bram gone to the beach in 2023?", "compose"),
        ("What books has Bram read?", "compose"),
        ("How many months passed between the two trips?", None),
        ("What was the first concert Ilse went to?", "replay"),
        ("When did Ilse go to the LGBTQ support group?", None),
        ("Why did Noor open a dance studio?", None),
        ("Where did Bram move from?", None),
    ],
)
def test_rule_read_mode(q: str, expected: str | None) -> None:
    """G24: the rules the decision planner applies before asking its provider."""
    from memspine.models.encoders.query.shape import rule_read_mode

    assert rule_read_mode(q) == expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What does Mirel say about the dancers in the photo?", True),
        ("What did Noor say about Mirel's progress with her store?", True),
        ("How do Bram and Ilse describe their journey together?", True),
        ("What did the posters at the poetry reading say?", True),
        ("What exactly did my landlord tell me about the deposit?", True),
        ("Can you give me her exact words?", True),
        ("What does Joost mention about his progress at the gym?", True),
        ("Which movie does Sven mention they enjoy watching?", False),
        ("Is the friend who wrote Anke the motivational quote alive?", False),
        ("What did Mirel design for her store?", False),
        ("When did Ilse say she would move?", False),
    ],
)
def test_is_verbatim(query: str, expected: bool) -> None:
    """F5 (plan v3.2): questions that ask for someone's own words."""
    from memspine.models.encoders.query.shape import is_verbatim

    assert is_verbatim(query) is expected


def test_feedback_terms_are_shared_words_the_query_lacks() -> None:
    """N03 (plan v3.2): pseudo-relevance feedback terms."""
    from memspine.models.encoders.query.shape import feedback_terms

    texts = [
        "Bram: we went camping at the lake with the tent",
        "Bram: the tent leaked at the lake again",
        "Ilse: painting by the lake is calming",
    ]
    assert feedback_terms("Where did Bram go camping?", texts) == "lake tent"
    assert feedback_terms("lake tent", texts) == ""


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What should I cook tonight?", True),
        ("Recommend a book for the flight", True),
        ("What did you say about my plan?", True),
        ("What is the capital of France?", False),
        ("How tall is the Eiffel Tower?", False),
    ],
)
def test_is_personal(query: str, expected: bool) -> None:
    """W9 (plan v3.2): personal vs general-knowledge questions."""
    from memspine.models.encoders.query.shape import is_personal

    assert is_personal(query) is expected


@pytest.mark.parametrize(
    ("query", "parts"),
    [
        (
            "What did Noor say about the studio, and where did he move?",
            ["What did Noor say about the studio", "where did he move"],
        ),
        ("What books has Bram read?", ["What books has Bram read"]),
        (
            "Recommend a hotel and also tell me the weather there in May",
            ["Recommend a hotel", "tell me the weather there in May"],
        ),
        ("Who did Ilse and Bram meet?", ["Who did Ilse and Bram meet"]),
    ],
)
def test_split_intents(query: str, parts: list[str]) -> None:
    """G34 (plan v3.2): multi-part questions."""
    from memspine.models.encoders.query.shape import split_intents

    assert split_intents(query) == parts
