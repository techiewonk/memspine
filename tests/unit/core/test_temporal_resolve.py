"""H1: deterministic relative-date resolution."""

from __future__ import annotations

from datetime import date

import pytest

from memspine.core.temporal_resolve import annotate, resolve

SAT = date(2023, 7, 15)  # a Saturday


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("yesterday", "Fri 2023-07-14"),
        ("last night", "Fri 2023-07-14"),
        ("today", "Sat 2023-07-15"),
        ("tomorrow", "Sun 2023-07-16"),
        ("the day before yesterday", "Thu 2023-07-13"),
        ("two days ago", "Thu 2023-07-13"),
        ("3 days ago", "Wed 2023-07-12"),
        ("last Friday", "Fri 2023-07-14"),
        ("next Saturday", "Sat 2023-07-22"),
        ("last week", "2023-07-03..2023-07-09"),
        ("last weekend", "2023-07-08..2023-07-09"),
        ("this weekend", "2023-07-15..2023-07-16"),
        ("last month", "2023-06"),
        ("next month", "2023-08"),
        ("last year", "2022"),
        ("a year ago", "≈ 2022"),
        ("three weeks ago", "≈ Sat 2023-06-24"),
    ],
)
def test_resolves_against_anchor(text: str, label: str) -> None:
    [r] = resolve(text, SAT)
    assert r.label == label


def test_last_weekday_on_that_weekday_means_the_previous_one() -> None:
    [r] = resolve("last Friday", date(2023, 7, 14))  # a Friday
    assert r.label == "Fri 2023-07-07"


def test_month_and_year_boundaries() -> None:
    [r] = resolve("yesterday", date(2024, 3, 1))  # leap year
    assert r.label == "Thu 2024-02-29"
    [r] = resolve("last month", date(2023, 1, 10))
    assert r.label == "2022-12"
    [r] = resolve("next month", date(2023, 12, 5))
    assert r.label == "2024-01"


def test_vague_phrases_are_left_alone() -> None:
    for text in ("recently", "the other day", "a few days ago", "some time ago"):
        assert resolve(text, SAT) == []


def test_annotate_inserts_after_each_phrase_and_keeps_text() -> None:
    out = annotate("I ran yesterday and painted last week.", SAT)
    assert out == (
        "I ran yesterday [= Fri 2023-07-14] and painted last week [= 2023-07-03..2023-07-09]."
    )
    assert annotate("nothing relative here", SAT) == "nothing relative here"


def test_longest_phrase_wins() -> None:
    [r] = resolve("the day before yesterday", SAT)
    assert r.phrase == "the day before yesterday"


# -- G13: anchored mode (LoCoMo's "the week before <session date>" convention) ---------


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ("last week", "the week before 2023-07-15 (2023-07-08..2023-07-14)"),
        ("past week", "the week before 2023-07-15 (2023-07-08..2023-07-14)"),
        ("next week", "the week after 2023-07-15 (2023-07-16..2023-07-22)"),
        ("this week", "the week of 2023-07-15 (2023-07-10..2023-07-16)"),
        ("last weekend", "the weekend before 2023-07-15 (2023-07-08..2023-07-09)"),
        ("this weekend", "the weekend of 2023-07-15 (2023-07-15..2023-07-16)"),
        ("next weekend", "the weekend after 2023-07-15 (2023-07-22..2023-07-23)"),
        ("last Friday", "the Friday before 2023-07-15 (Fri 2023-07-14)"),
        ("next Saturday", "the Saturday after 2023-07-15 (Sat 2023-07-22)"),
        ("two weeks ago", "two weeks before 2023-07-15 (≈ Sat 2023-07-01)"),
        ("a week ago", "a week before 2023-07-15 (≈ Sat 2023-07-08)"),
        ("a few days ago", "a few days before 2023-07-15"),
        ("few days ago", "a few days before 2023-07-15"),
        # Calendar units in the gold too: unchanged.
        ("last month", "2023-06"),
        ("next month", "2023-08"),
        ("last year", "2022"),
        ("yesterday", "Fri 2023-07-14"),
        ("two days ago", "Thu 2023-07-13"),
        ("this Friday", "Fri 2023-07-14"),
        ("a year ago", "≈ 2022"),
    ],
)
def test_anchored_mode_states_the_relation_to_the_anchor(text: str, label: str) -> None:
    [r] = resolve(text, SAT, anchored=True)
    assert r.label == label


def test_anchored_last_week_matches_locomo_gold_spans() -> None:
    """LoCoMo conv-26: "last week" said on 9 June 2023, gold "The week before 9 June 2023";
    "last weekend" said on Monday 17 July 2023, gold "The weekend before 17 July 2023"."""
    [r] = resolve("Last week", date(2023, 6, 9), anchored=True)
    assert (r.first, r.last) == (date(2023, 6, 2), date(2023, 6, 8))
    assert r.label == "the week before 2023-06-09 (2023-06-02..2023-06-08)"
    [r] = resolve("Last weekend", date(2023, 7, 17), anchored=True)
    assert r.label == "the weekend before 2023-07-17 (2023-07-15..2023-07-16)"


def test_anchored_annotate_and_default_unchanged() -> None:
    text = "I ran a few days ago and painted last week."
    assert annotate(text, SAT, anchored=True) == (
        "I ran a few days ago [= a few days before 2023-07-15] and painted last week "
        "[= the week before 2023-07-15 (2023-07-08..2023-07-14)]."
    )
    assert annotate(text, SAT) == (
        "I ran a few days ago and painted last week [= 2023-07-03..2023-07-09]."
    )
