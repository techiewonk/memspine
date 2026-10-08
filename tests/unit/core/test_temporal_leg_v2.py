"""N45 year inference, N44 soft window, N61 overlap ranking in the temporal leg."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.records import MemoryRecord
from memspine.core.temporal_query import query_interval, temporal_leg


def _d(y: int, m: int, d: int) -> datetime:
    return datetime(y, m, d, tzinfo=UTC)


def _rec(content: str, when: datetime) -> MemoryRecord:
    return MemoryRecord(namespace="a", memory_type="episodic", content=content, valid_from=when)


REF = _d(2023, 8, 20)


def test_keys_are_off_by_default() -> None:
    read = ReadConfig()
    assert (read.temporal_infer_year, read.temporal_rank, read.temporal_soft) == (
        False,
        "midpoint",
        False,
    )


@pytest.mark.parametrize(
    ("query", "span"),
    [
        ("what did she do on 7 May?", (_d(2023, 5, 7), _d(2023, 5, 8))),
        ("on May 7th", (_d(2023, 5, 7), _d(2023, 5, 8))),
        ("what happened in March?", (_d(2023, 3, 1), _d(2023, 4, 1))),
        # A date after the reference falls in the previous year.
        ("on 3 December", (_d(2022, 12, 3), _d(2022, 12, 4))),
        ("in November", (_d(2022, 11, 1), _d(2022, 12, 1))),
    ],
)
def test_year_is_inferred_from_the_reference(query: str, span: tuple) -> None:
    assert query_interval(query, year_ref=REF) == span


@pytest.mark.parametrize(
    "query",
    ["may I ask what she likes?", "they march on Sunday", "on 7 May"],
)
def test_without_reference_or_capitalised_month_nothing_is_inferred(query: str) -> None:
    ref = None if query == "on 7 May" else REF
    assert query_interval(query, year_ref=ref) is None


def test_an_explicit_year_still_wins() -> None:
    assert query_interval("on 7 May 2021", year_ref=REF) == (_d(2021, 5, 7), _d(2021, 5, 8))


def test_soft_fills_empty_slots_with_the_nearest_outside() -> None:
    recs = [
        _rec("in span", _d(2023, 5, 7)),
        _rec("a day after", _d(2023, 5, 8)),
        _rec("two days before", _d(2023, 5, 5)),
        _rec("far away", _d(2023, 7, 1)),
    ]
    hard = temporal_leg("on 7 May 2023", recs, 5)
    soft = temporal_leg("on 7 May 2023", recs, 5, soft=True)
    assert [h.record_id for h in hard] == [recs[0].record_id]
    assert [h.record_id for h in soft] == [r.record_id for r in recs[:3]]


def test_soft_never_displaces_an_in_span_record() -> None:
    recs = [_rec(f"turn {i}", _d(2023, 5, 7)) for i in range(3)] + [_rec("near", _d(2023, 5, 8))]
    soft = temporal_leg("on 7 May 2023", recs, 3, soft=True)
    assert {h.record_id for h in soft} == {r.record_id for r in recs[:3]}


def test_overlap_rank_puts_the_matching_turn_first() -> None:
    recs = [
        _rec("we had pasta for dinner", _d(2023, 5, 15)),  # closest to the middle
        _rec("Ana went camping at the lake", _d(2023, 5, 2)),
    ]
    mid = temporal_leg("when in May 2023 did Ana go camping?", recs, 2)
    over = temporal_leg("when in May 2023 did Ana go camping?", recs, 2, rank="overlap")
    assert mid[0].record_id == recs[0].record_id
    assert over[0].record_id == recs[1].record_id


async def test_engine_passes_the_newest_record_time_as_year_ref(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memspine.engine as engine_module

    seen: list[dict] = []
    real = engine_module.temporal_leg

    def spy(*args: object, **kwargs: object) -> list:
        seen.append(kwargs)
        return real(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(engine_module, "temporal_leg", spy)
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "temporal_leg": True, "temporal_infer_year": True},
    )
    await eng.start()
    try:
        for text, when in (("Ana: I went camping", _d(2023, 5, 7)), ("Ana: work", _d(2023, 8, 1))):
            await eng.write(text, namespace="a", memory_type="episodic", valid_from=when)
        hits = await eng.search("what happened on 7 May?", namespace="a", top_k=3)
    finally:
        await eng.stop()
    assert seen and seen[-1]["year_ref"] == _d(2023, 8, 1)
    assert any("camping" in r.content for r, _ in hits)
