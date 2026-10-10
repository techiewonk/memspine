"""E06: the code checks of an answer against its query contract (``core/answer_check``)."""

from __future__ import annotations

import pytest

from memspine.core.answer_check import (
    check_answer,
    defect_instruction,
    is_abstention,
    is_country,
)
from memspine.core.query_contract import build_contract


def kinds(question: str, answer: str, **kw: object) -> list[str]:
    return [d.kind for d in check_answer(build_contract(question), answer, **kw)]  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("question", "answer", "expected"),
    [
        ("When did Sam adopt the dog?", "Sam adopted the dog on 3 May 2023.", []),
        ("When did Sam adopt the dog?", "Sam adopted the dog last week.", []),
        ("When did Sam adopt the dog?", "Sam adopted a golden retriever.", ["missing_date"]),
        ("When did Sam adopt the dog?", "When Sam was ten years old.", []),
        ("What year did Sam graduate?", "Sam graduated in the spring.", ["missing_year"]),
        ("What year did Sam graduate?", "In 2019.", []),
        ("What time does Sam wake up?", "Sam wakes up very early.", ["missing_time"]),
        ("What time does Sam wake up?", "At 6:30 am.", []),
        ("How long did Sam live in Oslo?", "Sam lived in Oslo for three years.", []),
        ("How long did Sam live in Oslo?", "Sam lived in Oslo.", ["missing_duration"]),
        ("How many dogs does Sam have?", "Sam has two dogs.", []),
        ("How many dogs does Sam have?", "Sam has a labrador and a poodle.", ["missing_number"]),
        ("How often does Sam run?", "Several times a week.", []),
        ("Did Sam finish the marathon?", "Yes, Sam finished.", []),
        ("Did Sam finish the marathon?", "Sam finished in the rain.", ["no_polarity"]),
        ("Which book did Sam read?", "The Hobbit", []),
        ("Which book did Sam read?", "2019-05-04", ["wrong_type"]),
        ("Where did Sam travel?", "12 March", ["wrong_type"]),
    ],
)
def test_type_checks(question: str, answer: str, expected: list[str]) -> None:
    assert kinds(question, answer) == expected


def test_a_country_for_a_city_is_caught_but_a_city_with_its_country_is_not() -> None:
    q = "Which city did Sam move to?"
    assert kinds(q, "Italy") == ["wrong_granularity"]
    assert kinds(q, "Sam moved to the United States.") == ["wrong_granularity"]
    assert kinds(q, "California") == ["wrong_granularity"]
    assert kinds(q, "Rome") == []
    assert kinds(q, "Rome, Italy") == []
    assert kinds("Which country did Sam move to?", "Italy") == []
    assert is_country("in Costa Rica") and not is_country("Lisbon")


def test_where_without_a_subtype_is_not_judged_on_granularity() -> None:
    assert kinds("Where did Sam move to?", "Italy") == []


def test_count_must_match_its_list() -> None:
    q = "How many pets does Sam have?"
    assert kinds(q, "Sam has 3 pets: Rex, Tom, Bella, Max.") == ["count_list_mismatch"]
    assert kinds(q, "Sam has 3 pets: Rex, Tom, Bella.") == []
    assert kinds(q, "Sam has three pets:\n1. Rex\n2. Tom\n3. Bella\n4. Max") == [
        "count_list_mismatch"
    ]
    # a hedge that the list satisfies, and dates inside the list are not counts
    assert kinds(q, "At least 2 pets: Rex, Tom, Bella.") == []
    assert kinds(q, "Sam has two pets: Rex (adopted May 3, 2022) and Tom.") == []


def test_dropped_items_from_an_evidence_table_or_the_explanation() -> None:
    q = "What hobbies does Sam have?"
    assert kinds(q, "Painting and chess.", evidence_items=["painting", "chess", "pottery"]) == [
        "dropped_items"
    ]
    assert kinds(q, "Painting, chess and pottery.", evidence_items=["painting", "chess"]) == []
    explanation = "Evidence:\n- painting at the studio\n- chess club\n- pottery class\nAnswer: x"
    got = check_answer(build_contract(q), "Painting and chess club.", explanation=explanation)
    assert [d.kind for d in got] == ["dropped_items"]
    assert "pottery" in got[0].detail
    # a single-valued question never asks for a list
    assert kinds("What hobby does Sam like most?", "Chess.", evidence_items=["chess", "art"]) == []


@pytest.mark.parametrize(
    "answer",
    [
        "Not mentioned in the conversation.",
        "The memories do not state when Sam adopted the dog.",
        "It cannot be determined from the memories.",
        "Unknown.",
        "",
    ],
)
def test_an_abstention_is_never_a_defect(answer: str) -> None:
    for q in (
        "When did Sam adopt the dog?",
        "How many dogs does Sam have?",
        "Which city did Sam move to?",
        "Did Sam finish?",
    ):
        assert kinds(q, answer) == []
    assert is_abstention(answer) or answer == ""


def test_an_unknown_contract_has_no_defects() -> None:
    assert kinds("Tell me about Sam.", "Blah.") == []


def test_soft_signals_are_off_unless_asked() -> None:
    c = build_contract("Who gave Sam the guitar?")
    assert check_answer(c, "his mother") == []
    assert [d.kind for d in check_answer(c, "his mother", soft=True)] == ["soft_unnamed"]
    assert check_answer(c, "Maria Lopez", soft=True) == []
    cities = build_contract("Which cities has Sam visited?")
    soft = [d.kind for d in check_answer(cities, "Rome, Paris, Italy", soft=True)]
    assert soft == ["soft_mixed_granularity"]


def test_defect_instruction_names_each_defect() -> None:
    d = check_answer(build_contract("When did Sam adopt the dog?"), "A retriever.")
    assert defect_instruction(d) == "the question asks for a date or a time."
