"""C1 follow-up probe, C2 recent-conversation header, C6 per-leg score floors, C7 section
caption (generic field practice; all off by default)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core.query_shape import is_followup

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def test_keys_are_off_by_default() -> None:
    read = ReadConfig()
    assert read.recent_exchanges == 0
    assert read.leg_min_scores == {}
    assert read.section_captions is False
    assert read.followup_probe is False


@pytest.mark.parametrize(
    ("question", "followup"),
    [
        ("What about her sister?", True),
        ("and then?", True),
        ("She said what?", True),
        ("why?", True),
        ("When did Ana go camping by the lake?", False),
        ("Where does Ben live?", False),
    ],
)
def test_is_followup(question: str, followup: bool) -> None:
    assert is_followup(question) is followup


async def _engine(**read: Any) -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )
    await eng.start()
    turns = [
        "Ana: we went camping by the lake",
        "Ben: my sister lives in Leeds",
        "Ana: what about your sister?",
    ]
    for i, text in enumerate(turns):
        await eng.write(
            text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=i)
        )
    return eng


async def test_recent_header_shows_latest_turns_and_leaves_out_the_question() -> None:
    eng = await _engine(recent_exchanges=2)
    try:
        result = await eng.read(
            "Ana: what about your sister?", namespace="a", mode="replay", budget_tokens=800
        )
    finally:
        await eng.stop()
    contents = [r.content for r in result.context.records]
    recent = [c for c in contents if c.startswith(constants.RECENT_MARKER)]
    assert len(recent) == 1
    assert "my sister lives in Leeds" in recent[0]
    assert "what about your sister" not in recent[0]  # the in-flight question
    assert any(c.startswith("Ben: my sister") for c in contents)  # not hidden below


async def test_caption_marks_the_retrieved_part_when_headers_lead() -> None:
    eng = await _engine(recent_exchanges=1, section_captions=True)
    try:
        result = await eng.read("camping", namespace="a", mode="replay", budget_tokens=800)
    finally:
        await eng.stop()
    contents = [r.content for r in result.context.records]
    assert constants.RETRIEVED_CAPTION in contents
    caption_at = contents.index(constants.RETRIEVED_CAPTION)
    assert contents[caption_at - 1].startswith(constants.RECENT_MARKER)


async def test_leg_floor_drops_weak_vector_hits() -> None:
    eng = await _engine(hybrid=False, leg_min_scores={"vector": 2.0})  # above any cosine
    try:
        hits = await eng.search("camping by the lake", namespace="a", top_k=3)
    finally:
        await eng.stop()
    assert hits == []


async def test_followup_probe_adds_the_previous_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _engine(followup_probe=True)
    seen: list[Any] = []
    real = eng._search

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("probes"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(eng, "_search", spy)
    try:
        await eng.assemble("and her job?", namespace="a", budget_tokens=200)
    finally:
        await eng.stop()
    assert any(p and any("what about your sister" in x for x in p) for p in seen)
