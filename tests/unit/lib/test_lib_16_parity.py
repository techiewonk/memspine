"""LIB-16: local relative-phrase anchoring equals LIB-01 (read may not import derive)."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from memspine.derive import temporal as T
from memspine.read import contract_rules as C

BER = ZoneInfo("Europe/Berlin")
NOWS = [
    datetime(2025, 5, 14, 10, 30, tzinfo=UTC),  # a Wednesday
    datetime(2025, 5, 17, 23, 59, tzinfo=UTC),  # a Saturday
    datetime(2025, 5, 18, 0, 5, tzinfo=UTC),  # a Sunday
    datetime(2025, 1, 31, 12, 0, tzinfo=UTC),  # month-end arithmetic
    datetime(2025, 3, 30, 1, 30, tzinfo=BER),  # the Berlin DST day
    datetime(2024, 2, 29, 8, 0, tzinfo=UTC),  # leap day
]
PHRASES = [
    "yesterday", "today", "tomorrow", "the day before yesterday", "the day after tomorrow",
    "last week", "next week", "this week", "last month", "next month", "this month",
    "last year", "next year", "this year", "last weekend", "this weekend", "next weekend",
    "last friday", "next monday", "this sunday", "3 days ago", "two weeks ago", "5 months ago",
    "a year ago", "in 3 weeks", "in 2 months", "in 10 days", "3 hours ago", "in 30 minutes",
    "gestern", "heute", "vorgestern", "uebermorgen", "letzte woche", "naechsten monat",
    "dieses jahr", "letztes wochenende", "vor 2 tagen", "vor drei wochen", "letzten freitag",
    "gisteren", "vandaag", "overmorgen", "vorige week", "volgende maand", "dit jaar",
    "3 dagen geleden", "twee weken geleden", "over 3 weken", "volgende vrijdag",
]  # fmt: skip


def _fold(s: str) -> tuple[str, ...]:
    return tuple(re.findall(r"[a-z0-9]+", C._fold(s)))


@pytest.mark.parametrize("now", NOWS, ids=lambda n: n.isoformat())
@pytest.mark.parametrize("phrase", PHRASES)
def test_local_anchoring_equals_lib01(phrase: str, now: datetime) -> None:
    cand = T.normalise_relative_phrase(phrase)
    mine = C._parse_relative(_fold(phrase))
    if cand is None:
        assert mine is None, phrase
        return
    assert mine is not None, phrase
    ref = T.anchor(cand, source_time=now, tz=now.tzinfo)
    span = T.period(ref, tz=now.tzinfo)
    got = C._anchor_relative(mine, now)
    if span is None:
        assert got is None, phrase
        return
    assert got is not None, phrase
    assert (got[0], got[1], got[2]) == (span.lo, span.hi, ref.precision), phrase


def test_unsure_phrases_stay_unparsed_like_lib01() -> None:
    for phrase in ("past week", "afgelopen week", "next weekend soon"):
        assert T.normalise_relative_phrase(phrase) is None
        assert C._parse_relative(_fold(phrase)) is None


def test_signed_number_is_not_a_phrase() -> None:
    now = datetime(2025, 5, 14, 10, 0, tzinfo=UTC)
    c = C.build_contract("What was the delta -3 days ago?", now=now, lang_hint="en")
    assert c.time_view.kind == "unspecified"
