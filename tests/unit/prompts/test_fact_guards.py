"""#31 (Graphiti GR-11): attribute guards on mined facts, and #29 evidence-line citations."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.prompts.models import (
    FACT_FIELD_MAX_CHARS,
    ExtractedFact,
    ExtractedFacts,
    FactDates,
    fact_guard,
)


def _fact(**fields: Any) -> dict[str, Any]:
    return {"entity": "Melanie", "attribute": "hobby", "value": "Melanie paints", **fields}


def _kept(*items: dict[str, Any]) -> list[ExtractedFact]:
    return ExtractedFacts.model_validate({"facts": list(items)}).facts


@pytest.mark.parametrize(
    "bad",
    [
        _fact(value="Unknown"),
        _fact(value="N/A"),
        _fact(value="not mentioned."),
        _fact(value="<value>"),
        _fact(value="[name]"),
        _fact(entity="{entity}"),
        _fact(entity="unknown"),
        _fact(attribute="placeholder"),
        _fact(value="Let me think about which facts matter here"),
        _fact(value="I think Melanie likes painting"),
        _fact(value="<think>reasoning</think> Melanie paints"),
        _fact(value="Melanie paints </think>"),
        _fact(attribute="Okay, the user is asking about hobbies"),
        _fact(value="The user said she paints"),
        _fact(entity="x" * (FACT_FIELD_MAX_CHARS + 1)),
        _fact(attribute="y" * (FACT_FIELD_MAX_CHARS + 1)),
    ],
)
def test_reasoning_placeholders_and_overlong_keys_are_dropped(bad: dict[str, Any]) -> None:
    kept = _kept(bad, _fact(value="Melanie went camping on 2023-07-14"))
    assert [f.value for f in kept] == ["Melanie went camping on 2023-07-14"]


def test_a_long_value_is_cut_at_a_word_boundary() -> None:
    value = "Melanie listed her hobbies: " + "pottery and camping, " * 20
    [fact] = _kept(_fact(value=value))
    assert len(fact.value) <= FACT_FIELD_MAX_CHARS
    assert value.startswith(fact.value) and not fact.value.endswith((" ", ","))


@pytest.mark.parametrize(
    "good",
    [
        _fact(),
        _fact(value="Melanie's favourite book is 'Nothing is Impossible'"),
        _fact(value="Melanie thinks pottery is calming"),  # "thinks" is not "I think"
        _fact(value="Melanie has none of the old paintings left"),
        _fact(entity="Unknown Pleasures (album)"),
        _fact(value="x" * FACT_FIELD_MAX_CHARS),
    ],
)
def test_real_facts_pass_unchanged(good: dict[str, Any]) -> None:
    [fact] = _kept(good)
    assert (fact.entity, fact.attribute, fact.value) == (
        good["entity"],
        good["attribute"],
        good["value"],
    )


def test_guard_leaves_non_mappings_and_missing_fields_to_validation() -> None:
    assert fact_guard("junk") == "junk"
    assert fact_guard({"entity": "A"}) == {"entity": "A"}
    with pytest.raises(ValueError):
        ExtractedFacts.model_validate({"facts": [{"entity": "A"}]})  # unchanged behaviour


@pytest.mark.parametrize(
    ("raw", "turns"),
    [
        (None, []),
        ([], []),
        (3, [3]),
        ("3, 4", [3, 4]),
        (["[3]", 4, "4", 0, -1, "x"], [3, 4]),
    ],
)
def test_turns_are_tolerant(raw: Any, turns: list[int]) -> None:
    [fact] = _kept(_fact(turns=raw))
    assert fact.turns == turns


def test_turns_default_empty() -> None:
    assert ExtractedFact(entity="A", attribute="b", value="c").turns == []


def test_fact_dates_model() -> None:
    out = FactDates.model_validate({"dates": [{"index": 1, "date": "2023-05-05"}, {"index": 2}]})
    assert [(d.index, d.date) for d in out.dates] == [(1, "2023-05-05"), (2, None)]
