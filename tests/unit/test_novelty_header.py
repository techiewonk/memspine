"""G32 (plan v3.2): ``read.novelty_exclusions``: a request for something new."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core.query_shape import is_novelty


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("Recommend a book I haven't read", True),
        ("Suggest something different for dinner", True),
        ("Any new restaurants other than sushi places?", True),
        ("What book did I read last week?", False),
    ],
)
def test_is_novelty(query: str, expected: bool) -> None:
    assert is_novelty(query) is expected


def test_novelty_exclusions_defaults_off() -> None:
    assert ReadConfig().novelty_exclusions is False


async def _read(query: str, **read: Any) -> list[str]:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )
    await eng.start()
    try:
        await eng.write(
            "user likes: The Overstory",
            namespace="a",
            memory_type="semantic",
            entity="user",
            attribute="likes",
        )
        await eng.write("I finished a novel on the train", namespace="a", memory_type="episodic")
        out = await eng.read(query, namespace="a", mode="retrieve", top_k=2)
        return [r.content for r in out.context.records if constants.NOVELTY_TAG in r.tags]
    finally:
        await eng.stop()


async def test_novelty_question_gets_the_known_items_header() -> None:
    header = await _read("Recommend a book I haven't read", novelty_exclusions=True)
    assert header and constants.NOVELTY_MARKER in header[0]
    assert "The Overstory" in header[0]


async def test_other_questions_and_off_get_no_header() -> None:
    assert await _read("What book did I finish?", novelty_exclusions=True) == []
    assert await _read("Recommend a book I haven't read") == []
