"""F2 (plan v3.2): ``read.temporal_relative``: relative questions use the read time."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig

NOW = datetime(2023, 6, 14, 12, 0, tzinfo=UTC)
TURNS = [
    ("We talked about the new garden plans", datetime(2023, 6, 13, 18, 0, tzinfo=UTC)),
    ("We talked about the old car repairs", datetime(2023, 5, 2, 18, 0, tzinfo=UTC)),
    ("We talked about the holiday budget", datetime(2023, 4, 2, 18, 0, tzinfo=UTC)),
]
QUERY = "What did we talk about yesterday?"


async def _rank(**read: Any) -> int:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, "temporal_leg": True, **read},
    )
    await eng.start()
    try:
        eng._clock = lambda: NOW
        for text, when in TURNS:
            await eng.write(text, namespace="a", memory_type="episodic", valid_from=when)
        hits = await eng.search(QUERY, namespace="a", top_k=3)
        return [r.content for r, _ in hits].index(TURNS[0][0])
    finally:
        await eng.stop()


def test_temporal_relative_defaults_off() -> None:
    assert ReadConfig().temporal_relative is False


async def test_relative_question_ranks_the_day_before_the_read_first() -> None:
    off = await _rank()
    on = await _rank(temporal_relative=True)
    assert on == 0
    assert on <= off
