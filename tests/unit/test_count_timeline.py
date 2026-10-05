"""E3: count-question support (``query_shape.is_count``, ``read.count_timeline``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core.lead import (
    count_terms,
    distinct_occurrences,
    mentions_event,
    render_occurrences,
)
from memspine.core.query_shape import is_count
from memspine.core.records import MemoryRecord
from memspine.exceptions import ConfigError

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)
QUERY = "How many times has Melanie gone to the beach?"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("How many times has Melanie gone to the beach?", True),
        ("How many pets does Caroline have?", True),
        ("How often does John go hiking?", True),
        ("how many concerts did Jon attend in 2023", True),
        ("How many days ago did Gina move?", False),  # a duration: a date question
        ("How many years has Dave worked there?", False),
        ("When did Melanie go to the beach?", False),
        ("What books has Melanie read?", False),
    ],
)
def test_is_count(query: str, expected: bool) -> None:
    assert is_count(query) is expected


def test_count_terms_drop_names_count_words_and_numbers() -> None:
    assert count_terms(QUERY) == ["gone", "beach"]
    assert count_terms("How many concerts did Jon attend in 2023?") == ["concerts", "attend"]


def test_mentions_event_matches_half_the_terms_on_a_prefix() -> None:
    assert mentions_event("We spent the day at the beaches", ["gone", "beach"])
    assert not mentions_event("We went camping", ["gone", "beach"])
    assert not mentions_event("anything", [])


def _rec(text: str, day: int, session: str | None, minute: int = 0) -> tuple[MemoryRecord, str]:
    stamp = T0 + timedelta(days=day, minutes=minute)
    rec = MemoryRecord(
        namespace="a", memory_type="episodic", content=text, valid_from=stamp, group_id=session
    )
    return rec, text


def test_two_same_day_mentions_count_once() -> None:
    same_session = [
        _rec("Melanie: we went to the beach today", 0, "s1"),
        _rec("Melanie: the kids loved the beach, so much sand", 0, "s1", minute=3),
    ]
    assert len(distinct_occurrences(same_session)) == 1
    # Another session on the same day, restating the same trip: one occurrence too.
    restated = [
        _rec("Melanie: we went to the beach today", 0, "s1"),
        _rec("Melanie: we went to the beach today with the kids", 0, "s2"),
    ]
    assert len(distinct_occurrences(restated)) == 1


def test_two_different_days_count_twice() -> None:
    mentions = [
        _rec("Melanie: we went to the beach today", 9, "s2"),
        _rec("Melanie: we went to the beach today", 0, "s1"),
    ]
    kept = distinct_occurrences(mentions)
    assert len(kept) == 2
    assert render_occurrences(kept) == (
        f"{constants.COUNT_MARKER}\n"
        "- [said 2023-05-07] Melanie: we went to the beach today\n"
        "- [said 2023-05-16] Melanie: we went to the beach today"
    )


def test_count_share_joins_the_header_share_validator() -> None:
    with pytest.raises(ConfigError, match=r"read\.count_budget_share"):
        ReadConfig(
            cards="header", cards_budget_share=0.6, count_timeline=True, count_budget_share=0.4
        )
    ReadConfig(cards="header", cards_budget_share=0.6, count_budget_share=0.4)  # count off


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, "render": "dated", **read},
    )


_TURNS = [
    ("Melanie: we went to the beach with the kids", 0, "s1"),
    ("Melanie: the beach was sunny and the kids built sand castles", 0, "s1"),
    ("Melanie: back at the beach again this morning", 9, "s2"),
    ("Caroline: I painted a sunset at the lake", 9, "s2"),
]


async def _seeded(**read: Any) -> Engine:
    eng = _engine(**read)
    await eng.start()
    for i, (text, day, session) in enumerate(_TURNS):
        await eng.write(
            text,
            namespace="a",
            memory_type="episodic",
            valid_from=T0 + timedelta(days=day, minutes=i),
            group_id=session,
        )
    return eng


def _contents(records: list[MemoryRecord]) -> list[str]:
    return [r.content for r in records]


async def test_count_timeline_lists_distinct_dated_mentions() -> None:
    eng = await _seeded(count_timeline=True)
    try:
        for records in (
            (await eng.read(QUERY, namespace="a", mode="retrieve")).context.records,
            (await eng.assemble(QUERY, namespace="a")).records,
        ):
            [block] = [r for r in records if constants.COUNT_TAG in r.tags]
            assert block.content == (
                f"{constants.COUNT_MARKER}\n"
                "- [said 2023-05-07] Melanie: we went to the beach with the kids\n"
                "- [said 2023-05-16] Melanie: back at the beach again this morning"
            )
            assert len(block.source.parents) == 2
            # The mentions stay in the context; the block only points at them.
            assert any("sand castles" in c for c in _contents(records))
            volatile = [r for r in records if constants.LEAD_TAG not in r.tags]
            assert records.index(block) < records.index(volatile[0])
    finally:
        await eng.stop()


async def test_count_timeline_off_is_byte_identical() -> None:
    """Off (the default) and on-for-a-non-count-question change nothing."""
    outputs = []
    for read, query in (
        ({}, QUERY),
        ({"count_timeline": False}, QUERY),
        ({}, "Where did Melanie go with the kids?"),
        ({"count_timeline": True}, "Where did Melanie go with the kids?"),
    ):
        eng = await _seeded(**read)
        try:
            routed = await eng.read(query, namespace="a", mode="retrieve")
            assembled = await eng.assemble(query, namespace="a")
            outputs.append(
                (
                    query,
                    _contents(routed.context.records),
                    routed.context.tokens_used,
                    _contents(assembled.records),
                    assembled.tokens_used,
                )
            )
        finally:
            await eng.stop()
    assert outputs[0] == outputs[1]
    assert outputs[2] == outputs[3]
    assert not any(constants.COUNT_MARKER in c for out in outputs for c in out[1] + out[3])
