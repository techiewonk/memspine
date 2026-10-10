"""I79: bare-weekday annotation and the event-vs-said label (``read.relative_dates_weekdays``,
``read.relative_dates_happened``). Synthetic lines, no model."""

from __future__ import annotations

from datetime import date

import pytest

from memspine.config.schema import ReadConfig
from memspine.core.temporal_resolve import annotate

SUNDAY = date(2022, 7, 10)  # a Sunday


def wk(text: str, anchor: date = SUNDAY, **kw: bool) -> str:
    return annotate(text, anchor, anchored=True, weekdays=True, **kw)


def test_defaults_are_off_and_unchanged() -> None:
    cfg = ReadConfig()
    assert cfg.relative_dates_weekdays is False and cfg.relative_dates_happened is False
    text = "I won my fourth tournament on Friday and played yesterday"
    assert annotate(text, SUNDAY, anchored=True) == annotate(
        text, SUNDAY, anchored=True, weekdays=False, happened=False
    )
    assert "Friday [=" not in annotate(text, SUNDAY, anchored=True)


def test_past_weekday_resolves_to_the_friday_before_the_line_date() -> None:
    out = wk("I won my fourth video game tournament on Friday! It was awesome.")
    assert "on Friday [= the Friday before 2022-07-10 (Fri 2022-07-08)]!" in out


def test_future_weekday_resolves_forward() -> None:
    out = wk("We are going to play a match on Wednesday.")
    assert "on Wednesday [= the Wednesday after 2022-07-10 (Wed 2022-07-13)]" in out


def test_unanchored_form_is_the_bare_day() -> None:
    out = annotate("She went hiking on Saturday", SUNDAY, weekdays=True)
    assert out == "She went hiking on Saturday [= Sat 2022-07-09]"


@pytest.mark.parametrize(
    "text",
    [
        "Let's meet on Friday",  # no tense cue: left alone
        "I went there and I will go again on Friday",  # both tenses: ambiguous
        "We play chess on Fridays",  # a habit
        "I went to the market on Sunday",  # the anchor's own weekday
        "I'm so excited for the show on Friday",  # 'excited' is not a past cue
    ],
)
def test_ambiguous_weekdays_fail_closed(text: str) -> None:
    assert wk(text) == text


def test_last_weekday_is_not_read_twice() -> None:
    out = wk("I went hiking last Friday")
    assert out.count("[=") == 1 and "the Friday before 2022-07-10" in out


def test_event_label_is_distinct_from_the_said_date() -> None:
    plain = annotate("We left yesterday", SUNDAY)
    assert plain == "We left yesterday [= Sat 2022-07-09]"
    out = annotate("We left yesterday", SUNDAY, happened=True)
    assert out == "We left yesterday [= event Sat 2022-07-09]"


def test_event_label_leaves_duration_labels_alone() -> None:
    out = annotate(
        "She has been painting for 3 years", date(2023, 5, 20), durations=True, happened=True
    )
    assert out.endswith("[= since about 2020]")
