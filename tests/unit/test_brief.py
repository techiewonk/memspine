"""G-20: ``Engine.brief`` builds session-start context with no question."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from memspine import Engine
from memspine.config import constants

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


async def test_brief_shows_the_last_turns_oldest_first_within_budget() -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
    )
    await eng.start()
    try:
        for i in range(10):
            await eng.write(
                f"turn number {i}",
                namespace="u",
                memory_type="episodic",
                valid_from=T0 + timedelta(minutes=i),
            )
        await eng.write("other user turn", namespace="v", memory_type="episodic", valid_from=T0)
        blocks = await eng.brief("u", recent_turns=3)
        tiny = await eng.brief("u", budget_tokens=5)
    finally:
        await eng.stop()
    recent = [b for b in blocks if b.content.startswith(constants.BRIEF_RECENT_MARKER)]
    assert len(recent) == 1
    lines = recent[0].content.splitlines()[1:]
    assert lines == ["- turn number 7", "- turn number 8", "- turn number 9"]
    assert "other user" not in "\n".join(b.content for b in blocks)
    assert tiny == []
