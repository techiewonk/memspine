"""W17d rule-based correction detector (ADR-060)."""

from __future__ import annotations

import pytest

from memspine.core.correction import detect_correction


@pytest.mark.parametrize(
    ("text", "replacement", "negated"),
    [
        ("No, I said Tuesday, not Monday.", "Tuesday", "Monday"),
        ("Actually it's 7 pm", "7 pm", None),
        ("not Monday, Tuesday", "Tuesday", "Monday"),
        ("It's not Berlin but Munich", "Munich", "Berlin"),
        ("Correction: the meeting is at noon", "the meeting is at noon", None),
        ("nope, it's blue", "blue", None),
        ("I meant the red one", "the red one", None),
    ],
)
def test_detects_corrections(text: str, replacement: str, negated: str | None) -> None:
    found = detect_correction(text)
    assert found is not None
    assert found.replacement == replacement
    assert found.negated == negated


def test_bare_thats_wrong_has_no_replacement() -> None:
    found = detect_correction("That's wrong.")
    assert found is not None and found.replacement is None and found.cue == "thats_wrong"


@pytest.mark.parametrize(
    "text",
    [
        "No thanks, I'm fine",
        "no problem at all",
        "She said 'no, it's Friday'",
        "I love hiking on weekends",
        "",
    ],
)
def test_skips_non_corrections(text: str) -> None:
    assert detect_correction(text) is None
