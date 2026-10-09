"""C5: read-time duration annotations (``read.resolve_durations``)."""

from __future__ import annotations

from datetime import date

import pytest

from memspine.core.temporal_resolve import annotate

ANCHOR = date(2023, 5, 20)


def dur(text: str) -> str:
    return annotate(text, ANCHOR, durations=True)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "She has been painting for 3 years",
            "She has been painting for 3 years [= since about 2020]",
        ),
        (
            "She has been painting for three years",
            "She has been painting for three years [= since about 2020]",
        ),
        ("I've had it for twelve years", "I've had it for twelve years [= since about 2011]"),
        ("been running for 6 months", "been running for 6 months [= since about 2022-11]"),
        ("running for 6 months now", "running for 6 months now [= since about 2022-11]"),
        ("Dancing 3 years now", "Dancing 3 years now [= since about 2020]"),
        ("Dating a month now", "Dating a month now [= since about 2023-04]"),
        ("Training two weeks now", "Training two weeks now [= since about 2023-05-06]"),
        ("Training 4 days now", "Training 4 days now [= since about 2023-05-16]"),
        ("since 2019", "since 2019 [= about 4 years as of 2023-05-20]"),
        ("since March 2019", "since March 2019 [= about 4 years 2 months as of 2023-05-20]"),
        ("since January 2023", "since January 2023 [= about 4 months as of 2023-05-20]"),
    ],
)
def test_duration_patterns(text: str, expected: str) -> None:
    assert dur(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "She has been painting for years",
        "She has been away for a while",
        "He stayed there for 3 years",  # finished span, no ongoing cue
        "I have to stay for 2 months",
        "since 2023",  # same year: no whole span
        "since May 2023",  # same month
        "since 2030",
        "for 3 days",
    ],
)
def test_ambiguous_text_is_left_alone(text: str) -> None:
    assert dur(text) == text


def test_anchor_is_the_memory_date() -> None:
    text = "has been here for 3 years"
    assert annotate(text, date(2019, 1, 1), durations=True).endswith("[= since about 2016]")
    assert annotate(text, date(2024, 3, 9), durations=True).endswith("[= since about 2021]")


def test_flag_off_changes_nothing() -> None:
    text = "has been painting for 3 years now, since 2019, a month now"
    assert annotate(text, ANCHOR) == text
    assert annotate(text, ANCHOR, durations=False) == text


def test_ago_keeps_its_base_annotation() -> None:
    out = dur("She started 3 years ago and has been painting for 3 years")
    assert "3 years ago [= ≈ 2020]" in out
    assert out.endswith("for 3 years [= since about 2020]")


def test_composes_with_base_phrases_and_anchored_mode() -> None:
    out = annotate("Last year she began; two weeks ago too", ANCHOR, anchored=True, durations=True)
    assert "[= 2022]" in out
    assert "two weeks before 2023-05-20" in out
