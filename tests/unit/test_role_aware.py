"""W11 (plan v3.2): ``read.role_aware``: assistant-side memory."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.temporal_query import (
    RECOMMENDATION_TAG,
    asks_about_assistant,
    is_recommendation,
)


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What book did you recommend last week?", True),
        ("Remind me of your suggestion for dinner", True),
        ("What did I say about the book?", False),
        ("Which book did Maria recommend?", False),
    ],
)
def test_asks_about_assistant(query: str, expected: bool) -> None:
    assert asks_about_assistant(query) is expected


def test_is_recommendation() -> None:
    assert is_recommendation("I'd recommend 'The Overstory' by Richard Powers.")
    assert is_recommendation("You should try the ramen place on 5th.")
    assert not is_recommendation("That sounds like a lovely trip.")


def test_role_aware_defaults_off() -> None:
    assert ReadConfig().role_aware is False


async def test_assistant_recommendation_is_found_for_a_you_recommended_question() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, "role_aware": True},
    )
    await eng.start()
    try:
        t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
        turns = [
            ("user", "I need a new novel for the flight, something about trees"),
            ("assistant", "I'd recommend The Overstory by Richard Powers."),
            ("user", "My sister read a thriller about trees last year"),
            ("assistant", "That sounds like a fun flight."),
        ]
        msgs = [
            {"role": r, "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
            for i, (r, c) in enumerate(turns)
        ]
        await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
        records = await eng._require_started().list_records("a", "episodic")
        tagged = [r.content for r in records if RECOMMENDATION_TAG in r.tags]
        assert tagged == ["I'd recommend The Overstory by Richard Powers."]
        hits = await eng.search("Which novel did you recommend for the flight?", namespace="a")
        assert hits[0][0].content == "I'd recommend The Overstory by Richard Powers."
    finally:
        await eng.stop()
