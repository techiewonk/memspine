"""N13 (plan v3.2): query-anchored excerpts of long records."""

from __future__ import annotations

from memspine.core.excerpt import ELLIPSIS, focused_excerpt

NOTE = "\n".join(
    [
        "Weekly team meeting notes",
        "Attendees: Ana, Ben, Chloe",
        "Budget review: marketing spend is up 10 percent",
        "The new office lease starts in March",
        "Lease deposit is 4,000 euros, paid by the company",
        "Hiring: two backend roles open",
        "Next meeting on Friday",
    ]
)


def test_short_text_is_unchanged() -> None:
    assert focused_excerpt("one line\ntwo lines", "lease") == "one line\ntwo lines"


def test_keeps_best_lines_and_their_followers_in_order() -> None:
    out = focused_excerpt(NOTE, "When does the office lease start?")
    lines = out.splitlines()
    assert "The new office lease starts in March" in lines
    assert "Lease deposit is 4,000 euros, paid by the company" in lines
    assert lines[0] == ELLIPSIS
    assert "Attendees: Ana, Ben, Chloe" not in lines
    assert len(out) < len(NOTE)


def test_no_shared_word_leaves_the_text_alone() -> None:
    assert focused_excerpt(NOTE, "Who won the football match?") == NOTE
