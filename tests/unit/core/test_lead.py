"""N18 (plan v3.2): standing-preference cues (``core/lead.py``)."""

from __future__ import annotations

import pytest


@pytest.mark.parametrize(
    "text",
    [
        "I strictly avoid caffeine after noon.",
        "I can't stand horror films.",
        "I'm vegetarian, so no meat dishes please.",
        "I find long video lectures ineffective.",
        "I have a phobia of elevators.",
        "I refuse to take public transport abroad.",
        "My favourite cuisine is Thai.",
        "I'm not a fan of loud bars.",
    ],
)
def test_wide_cues_catch_first_person_preferences(text: str) -> None:
    from memspine.core.lead import is_standing_instruction

    assert not is_standing_instruction(text)
    assert is_standing_instruction(text, wide=True)


@pytest.mark.parametrize(
    "text",
    [
        "I love how you explained the tax rules.",
        "We went hiking at the lake yesterday.",
        "The meeting moved to Friday.",
    ],
)
def test_wide_cues_leave_compliments_and_events_alone(text: str) -> None:
    from memspine.core.lead import is_standing_instruction

    assert not is_standing_instruction(text, wide=True)


def test_standing_patterns_default_narrow() -> None:
    from memspine.config.schema import ReadConfig

    assert ReadConfig().standing_patterns == "narrow"
