"""W12 (plan v3.2, ADR-061): the deterministic causal and kinship rules."""

from __future__ import annotations

import pytest

from memspine.core.rule_edges import (
    CausalClause,
    KinshipRelation,
    causal_clauses,
    causal_links,
    is_why_question,
    kinship_relations,
    strip_speaker,
)


def test_strip_speaker() -> None:
    assert strip_speaker("Caroline: I moved") == "I moved"
    assert strip_speaker("no speaker here") == "no speaker here"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "I quit the band because the rehearsals clashed with work.",
            CausalClause("because", "the rehearsals clashed with work.", "I quit the band"),
        ),
        (
            "Because the rehearsals clashed with work, I quit the band.",
            CausalClause("because", "the rehearsals clashed with work", "I quit the band."),
        ),
        (
            "The rehearsals clashed with work, so I quit the band.",
            CausalClause("so", "The rehearsals clashed with work", "I quit the band."),
        ),
        (
            "Mel: I chose them 'cause they help folks with adoption.",
            CausalClause("'cause", "they help folks with adoption.", "I chose them"),
        ),
    ],
)
def test_causal_clauses_split_cause_and_effect(text: str, expected: CausalClause) -> None:
    assert causal_clauses(text) == [expected]


@pytest.mark.parametrize(
    "text",
    ["I am so happy for you!", "That was so much fun", "No connective in this one."],
)
def test_intensifier_so_and_plain_text_are_not_causal(text: str) -> None:
    assert causal_clauses(text) == []


def test_cause_clause_links_to_the_earlier_turn_it_names() -> None:
    turns = [
        ("t0", "Ana: The rehearsals clashed with my shifts at work."),
        ("t1", "Bo: That sounds hard."),
        ("t2", "Ana: Anyway, I quit the band because the rehearsals clashed with work."),
    ]
    links = causal_links(turns)
    assert [(link.src, link.dst, link.cue) for link in links] == [("t2", "t0", "because")]
    assert links[0].overlap >= 2


def test_effect_clause_links_from_the_earlier_turn() -> None:
    turns = [
        ("t0", "Ana: I adopted a rescue dog called Biscuit last week."),
        ("t1", "Ana: My flat felt empty, so I adopted a rescue dog."),
    ]
    assert [(link.src, link.dst) for link in causal_links(turns)] == [("t0", "t1")]


def test_answer_to_a_why_question_is_its_cause() -> None:
    turns = [
        ("q", "Mel: That agency looks great! What made you pick it?"),
        ("a", "Caroline: They support LGBTQ+ folks with adoption."),
        ("x", "Mel: Lovely."),
    ]
    links = causal_links(turns)
    assert [(link.src, link.dst, link.cue) for link in links] == [("q", "a", "why?")]


def test_because_and_thats_why_openings_link_adjacent_turns() -> None:
    turns = [
        ("a", "Ana: I stopped running."),
        ("b", "Ana: Because my knee hurt."),
        ("c", "Bo: My knee hurt too."),
        ("d", "Bo: That's why I swim now."),
    ]
    pairs = {(link.src, link.dst) for link in causal_links(turns)}
    assert ("a", "b") in pairs
    assert ("d", "c") in pairs


def test_lookback_and_min_overlap_bound_the_search() -> None:
    turns = [
        ("t0", "the rehearsals clashed with work"),
        ("t1", "filler"),
        ("t2", "I quit because the rehearsals clashed with work"),
    ]
    assert causal_links(turns, lookback=1) == []
    assert causal_links(turns, min_overlap=10) == []


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Why did Caroline choose the agency?", True),
        ("What made Jon start a dance studio?", True),
        ("How come she moved?", True),
        ("When did Caroline move?", False),
    ],
)
def test_is_why_question(query: str, expected: bool) -> None:
    assert is_why_question(query) is expected


def test_kinship_relations() -> None:
    text = "Caroline: My sister Ana visited, and Bob, my boss, called. Tia is my mentor."
    assert kinship_relations(text) == [
        KinshipRelation("Ana", "sister", "caroline"),
        KinshipRelation("Bob", "boss", "caroline"),
        KinshipRelation("Tia", "mentor", "caroline"),
    ]


def test_kinship_owner_defaults_to_user_and_skips_clause_words() -> None:
    assert kinship_relations("my co-worker Sam left") == [
        KinshipRelation("Sam", "coworker", "user")
    ]
    assert kinship_relations("And my friend") == []
