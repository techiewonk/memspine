"""Conservative non-generative attribution/modality checks for rule candidates."""

import pytest

from memspine.core.rule_miner import mine_rules


def slots(text: str) -> set[tuple[str, str]]:
    return {(fact.attribute, fact.value) for fact in mine_rules(f"[7] [2026-10-01] Asha: {text}")}


@pytest.mark.parametrize(
    "text",
    [
        "If I live in Lisbon, I will need a new job.",
        "I live in Lisbon if I get the job.",
        "Unless I live in Lisbon, I cannot take that role.",
        "Suppose I live in Lisbon.",
        "Imagine I live in Lisbon.",
        "Maybe I live in Lisbon.",
        "Perhaps I live in Lisbon.",
        "I think I live in Lisbon.",
        "I live in Lisbon, I think.",
        "I live in Lisbon, allegedly.",
        "I dreamed that I live in Lisbon.",
        "I wish I lived in Lisbon.",
        "I live in Porto or I live in Lisbon.",
        "Either I live in Porto or I live in Lisbon.",
        "I live in Porto, or Lisbon.",
        "I don’t think I live in Porto.",
        "I don’t believe I live in Porto.",
        "It isn’t true that I live in Porto.",
        "I don't think I live in Lisbon.",
        "It is not true that I live in Lisbon.",
        "It is not the case that I live in Lisbon.",
        "It is false that I live in Lisbon.",
        "It is not that I live in Lisbon.",
        "I live in Lisbon?",
        "Do I live in Lisbon?",
        'Daniel said, "I live in Porto."',
        "Daniel said, 'I live in Porto.'",
        "Daniel said, “I live in Porto.”",
        "Daniel said, ‘I live in Porto.’",
        "Daniel said I live in Porto.",
        "Daniel told Maya that I live in Porto.",
        "According to Daniel, I live in Porto.",
        "Daniel asked whether I live in Porto.",
        "Daniel suggested that I live in Porto.",
        "Daniel said I live in Porto and I work at Acme.",
        "If I live in Porto and I work at Acme, things could change.",
        'For example, "I live in Porto."',
        "Example: `I live in Porto.`",
        'Daniel said "I live in Porto',
        "I do not live in Porto.",
        "I might live in Porto next year.",
        "I would live in Porto.",
        "I should live in Porto.",
        "I plan to say I live in Porto.",
        "If I moved to Porto, I would buy a house.",
        "I moved to Porto if I remember correctly.",
    ],
)
def test_nonassertive_or_reported_home_is_not_proposed(text: str) -> None:
    assert not any(attribute in {"home", "employer"} for attribute, _ in slots(text))


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("I live in Lisbon.", {("home", "Lisbon")}),
        ("I recently moved to Lisbon.", {("home", "Lisbon")}),
        ("I've moved to Lisbon.", {("home", "Lisbon")}),
        ("I'm from Porto.", {("origin", "Porto")}),
        ("I'm vegetarian.", {("diet", "vegetarian")}),
        ("My favorite book is 'Dune'.", {("favourite_book", "Dune")}),
        ("My favorite book is Charlotte's Web.", {("favourite_book", "Charlotte's Web")}),
        ("My friends' dog is friendly. I live in Lisbon.", {("home", "Lisbon")}),
        ("I live in Lisbon and I work at Acme.", {("home", "Lisbon"), ("employer", "Acme")}),
        ("I live in Lisbon because I work nearby.", {("home", "Lisbon")}),
        ("Either Daniel goes or Maya stays, but I live in York.", {("home", "York")}),
        ("I live in Lisbon, but if I get the job I may move.", {("home", "Lisbon")}),
        ("I live in Lisbon, but maybe I will move.", {("home", "Lisbon")}),
        ("Daniel said I live in Porto, but I live in York.", {("home", "York")}),
        ('Daniel said, "I live in Porto." I live in York.', {("home", "York")}),
        ("If I live in Porto, I will move. I live in York.", {("home", "York")}),
    ],
)
def test_supported_independent_assertions_survive(
    text: str, expected: set[tuple[str, str]]
) -> None:
    assert slots(text) == expected


def test_explicit_plans_stay_plans_and_never_become_completed_moves() -> None:
    assert slots("I plan to move to Lisbon.") == {("plans", "move to Lisbon")}
    assert slots("I'm planning to move to Porto.") == {("plans", "move to Porto")}


@pytest.mark.parametrize(
    "text",
    [
        "If my favorite book is Dune, I might read it again.",
        "Daniel says my mother is called Maya.",
        'Daniel said, "I no longer like coffee."',
        "If I no longer like coffee, I will try tea.",
    ],
)
def test_all_special_rule_families_use_the_same_scope_guard(text: str) -> None:
    assert slots(text) == set()


def test_source_line_date_and_transcript_speaker_are_preserved() -> None:
    [fact] = mine_rules("[17] [2026-10-01] Asha: I live in Lisbon.")
    assert (fact.entity, fact.date, fact.turns, fact.persons) == (
        "Asha",
        "2026-10-01",
        [17],
        ["Asha"],
    )


def test_explicit_negative_preference_remains_a_supported_negative() -> None:
    assert ("dislikes", "coffee") in slots("I don't like coffee.")
