"""#29: happened (event) dates of mined facts and their card rendering; #60 event days."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from memspine.core.event_date import (
    cited_turns,
    happened_label,
    happened_of,
    happened_tag,
    label_start,
    normalise_label,
    resolve_happened,
    said_differs,
)
from memspine.core.lead import card_line, event_day
from memspine.core.records import MemoryRecord

MON = date(2023, 5, 8)


@pytest.mark.parametrize(
    ("first", "last", "label"),
    [
        (date(2023, 7, 14), date(2023, 7, 14), "2023-07-14"),
        (date(2023, 6, 1), date(2023, 6, 30), "2023-06"),
        (date(2023, 12, 1), date(2023, 12, 31), "2023-12"),
        (date(2022, 1, 1), date(2022, 12, 31), "2022"),
        (date(2023, 6, 2), date(2023, 6, 8), "2023-06-02..2023-06-08"),
        (date(2023, 6, 2), date(2023, 7, 31), "2023-06-02..2023-07-31"),
    ],
)
def test_happened_label(first: date, last: date, label: str) -> None:
    assert happened_label(first, last) == label
    assert label_start(label) == first


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("2023-07-14", "2023-07-14"),
        (" 2023-07 ", "2023-07"),
        ("2023", "2023"),
        ("2023-06-02..2023-06-08", "2023-06-02..2023-06-08"),
        ("July 2023", None),
        ("", None),
        (None, None),
        ("last week", None),
    ],
)
def test_normalise_label(raw: str | None, label: str | None) -> None:
    assert normalise_label(raw) == label


def test_resolve_happened_needs_one_exact_span() -> None:
    assert resolve_happened([("I went camping last Friday", MON)]) == (
        date(2023, 5, 5),
        date(2023, 5, 5),
    )
    # Each text against its own anchor; agreeing resolutions are one span.
    assert resolve_happened(
        [("camping last Friday", MON), ("camping yesterday", date(2023, 5, 6))]
    ) == (date(2023, 5, 5), date(2023, 5, 5))
    # Disagreeing, approximate or absent: no date (a wrong date is worse than none).
    assert resolve_happened([("yesterday and last Friday", MON)]) is None
    assert resolve_happened([("three weeks ago", MON)]) is None
    assert resolve_happened([("no time here", MON)]) is None
    # #58: the week mode applies.
    assert resolve_happened([("last week", MON)], week="preceding_7_days") == (
        date(2023, 5, 1),
        date(2023, 5, 7),
    )


def _fact(tags: list[str] | None = None) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content="Melanie event: Melanie went camping with her kids",
        entity="Melanie",
        tags=tags or [],
    )


def test_happened_tag_round_trip() -> None:
    assert happened_of(_fact([happened_tag("2023-05-05"), "atomic_fact"])) == "2023-05-05"
    assert happened_of(_fact(["atomic_fact"])) is None
    assert said_differs(MON, "2023-05-05") and not said_differs(MON, "2023-05-08")


def test_cited_turns_ignores_out_of_range_lines() -> None:
    members = [_fact(), _fact(), _fact()]
    assert cited_turns(members, [2, 0, 9, 3]) == [members[1], members[2]]
    assert cited_turns(members, []) == []


def test_card_line_renders_said_and_happened_only_when_they_differ() -> None:
    said = datetime(2023, 5, 8, 13, tzinfo=UTC)
    fact = _fact()
    plain = "[said 2023-05-08] Melanie: Melanie went camping with her kids"
    assert card_line(fact, said) == plain
    assert card_line(fact, said, happened=None) == plain
    assert card_line(fact, said, happened="2023-05-08") == plain  # same day: unchanged
    assert card_line(fact, said, happened="2023-05-05") == (
        "[said 2023-05-08 · happened 2023-05-05] Melanie: Melanie went camping with her kids"
    )
    # No said date: no bracket at all (unchanged).
    assert card_line(fact, None, happened="2023-05-05") == (
        "Melanie: Melanie went camping with her kids"
    )


def test_event_day_is_the_single_named_day_else_the_said_day() -> None:
    said = datetime(2023, 5, 8, 13, tzinfo=UTC)
    assert event_day("I went to the beach yesterday", said) == date(2023, 5, 7)
    assert event_day("the beach last Friday", said) == date(2023, 5, 5)
    assert event_day("the beach", said) == MON
    assert event_day("the beach last week", said) == MON  # a span, not a day
    assert event_day("yesterday or last Friday", said) == MON  # ambiguous
