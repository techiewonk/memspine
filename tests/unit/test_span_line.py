"""N10 (plan v3.2): ``read.span_line``: the engine computes the time between events."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core.query_shape import is_duration


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("How long after the move did Ana adopt the dog?", True),
        ("How many weeks passed between the race and the trip?", True),
        ("How many dogs does Ana have?", False),
        ("When did Ana move?", False),
    ],
)
def test_is_duration(query: str, expected: bool) -> None:
    assert is_duration(query) is expected


async def _read(query: str, **read: Any) -> list[str]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )
    await eng.start()
    try:
        for text, when in (
            ("Ana: I moved to Lyon today", datetime(2023, 3, 1, tzinfo=UTC)),
            ("Ana: we adopted a dog called Pip", datetime(2023, 4, 12, tzinfo=UTC)),
            ("Ana: the weather is grey", datetime(2023, 4, 20, tzinfo=UTC)),
        ):
            await eng.write(text, namespace="a", memory_type="episodic", valid_from=when)
        out = await eng.read(query, namespace="a", mode="retrieve", top_k=3)
        return [r.content for r in out.context.records if constants.SPAN_TAG in r.tags]
    finally:
        await eng.stop()


async def test_span_line_computes_the_days_between_the_two_events() -> None:
    lines = await _read("How long after Ana moved to Lyon did she adopt the dog?", span_line=True)
    assert lines and "42 days" in lines[0]
    assert "2023-03-01" in lines[0] and "2023-04-12" in lines[0]


async def test_off_or_other_questions_get_no_span_line() -> None:
    assert await _read("How long after Ana moved to Lyon did she adopt the dog?") == []
    assert await _read("Where did Ana move?", span_line=True) == []
    assert ReadConfig().span_line is False
