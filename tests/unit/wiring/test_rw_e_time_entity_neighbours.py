"""RW-E: R3-14 time view, R3-05a entity leg v2, R3-10 neighbours v2 wiring (flag ON paths)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.read_filters import DateFilter, as_of_scope, date_filter_scope
from memspine.read.pipeline import timeview as pv
from memspine.read.pipeline.timeview import _timeview_allowed
from memspine.store.records import MemoryRecord, RecordStatus

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True},
            "associative": {"enabled": True, "policies": {"entity_nodes": True}},
        },
        read={"hybrid": False, "record_access": False, **read},
    )


def test_flags_default_off() -> None:
    cfg = ReadConfig()
    assert not (cfg.timeview_v2 or cfg.entity_leg_v2 or cfg.neighbours_v2)


# ------------------------------------------------------------------ R3-14


def _rec(rid: str, start: datetime, end: datetime | None = None, **kw: Any) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid,
        namespace="a",
        memory_type="semantic",
        content=rid,
        valid_from=start,
        valid_to=end,
        recorded_at=kw.pop("recorded_at", start),
        **kw,
    )


def test_timeview_true_at_uses_event_time_and_drops_ended() -> None:
    old = _rec("old", T0, T0 + timedelta(days=10), status=RecordStatus.ARCHIVED)
    new = _rec("new", T0 + timedelta(days=10))
    later = _rec("later", T0 + timedelta(days=30))
    with as_of_scope(T0 + timedelta(days=15)):
        assert _timeview_allowed([old, new, later], None, None) == {"new"}
    with as_of_scope(T0 + timedelta(days=5)):
        assert _timeview_allowed([old, new, later], None, None) == {"old"}


def test_timeview_known_at_uses_recorded_time() -> None:
    a = _rec("a", T0, recorded_at=T0)
    b = _rec("b", T0, recorded_at=T0 + timedelta(days=3))
    f = DateFilter.build(recorded_before=T0 + timedelta(days=1))
    with date_filter_scope(f):
        assert _timeview_allowed([a, b], f, None) == {"a"}


def test_timeview_both_axes_and_no_bound_falls_back() -> None:
    a = _rec("a", T0, recorded_at=T0)
    b = _rec("b", T0 - timedelta(days=9), recorded_at=T0 + timedelta(days=3))
    f = DateFilter.build(recorded_before=T0 + timedelta(days=1))
    with as_of_scope(T0 + timedelta(days=1)):
        assert _timeview_allowed([a, b], f, None) == {"a"}
    assert _timeview_allowed([a, b], None, None) is None
    assert _timeview_allowed([a], DateFilter.build(valid_from_after=T0), None) is None


async def test_timeview_flag_off_is_the_legacy_filter() -> None:
    eng = _engine()
    await eng.start()
    try:
        r = await eng.write("x", namespace="a", memory_type="episodic", valid_from=T0)
        f = DateFilter.build(valid_from_before=T0 + timedelta(days=1))
        with date_filter_scope(f):
            got = await pv._date_allowed(eng, "a")
        assert got is not None
        assert got[0] == {r.record_id} and got[1] == 1
    finally:
        await eng.stop()


# ------------------------------------------------------------------ R3-05a


async def test_entity_leg_v2_scores_named_entity_records_and_caps() -> None:
    eng = _engine(entity_leg_v2=True)
    await eng.start()
    try:
        a = await eng.write(
            "Melanie painted a lake", namespace="a", memory_type="episodic", entity="Melanie"
        )
        await eng.write(
            "Caleb bought a truck", namespace="a", memory_type="episodic", entity="Caleb"
        )
        legs = await eng._metadata_legs("a", "what did Melanie do", 10)
        named = [leg for leg in legs if getattr(leg, "name", "") == "entity_v2"]
        assert len(named) == 1
        assert [h.record_id for h in named[0]] == [a.record_id]
        assert 0 < named[0][0].score <= 0.5  # R-08: boost 0.5, capped
    finally:
        await eng.stop()


async def test_entity_leg_v2_off_adds_no_leg() -> None:
    eng = _engine()
    await eng.start()
    try:
        await eng.write("Melanie painted", namespace="a", memory_type="episodic", entity="Melanie")
        assert await eng._metadata_legs("a", "what did Melanie do", 10) == []
    finally:
        await eng.stop()


# ------------------------------------------------------------------ R3-10


async def _session(eng: Engine, monkeypatch: pytest.MonkeyPatch) -> list[MemoryRecord]:
    turns = [
        await eng.write(
            f"turn {i} of the talk",
            namespace="a",
            memory_type="episodic",
            valid_from=T0 + timedelta(minutes=i),
        )
        for i in range(7)
    ]

    async def fake_search(
        probe: str, namespace: str = "a", top_k: int = 8, **_: Any
    ) -> list[tuple[MemoryRecord, float]]:
        return [(turns[3], 0.9)]

    monkeypatch.setattr(eng, "_search", fake_search)
    return turns


async def test_neighbours_v2_matches_the_window_and_respects_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(compose_replay=True, neighbours_v2=True)
    await eng.start()
    try:
        turns = await _session(eng, monkeypatch)
        out = await eng.read(
            "how many turns", namespace="a", mode="compose", top_k=1, replay_window=2
        )
        assert [r.record_id for r in out.context.records] == [t.record_id for t in turns[1:6]]
        cost = len(turns[0].content) // 4 + 1
        small = await eng.read(
            "how many turns",
            namespace="a",
            mode="compose",
            top_k=1,
            replay_window=2,
            budget_tokens=cost * 3,
        )
        got = [r.record_id for r in small.context.records]
        assert turns[3].record_id in got and len(got) <= 3
    finally:
        await eng.stop()


async def test_neighbours_v2_drops_invisible_neighbours(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(compose_replay=True, neighbours_v2=True)
    await eng.start()
    try:
        turns = await _session(eng, monkeypatch)
        hidden = turns[4].record_id
        real = eng._replay_neighbour

        async def gated(rid: str, ns: str) -> MemoryRecord | None:
            return None if rid == hidden else await real(rid, ns)

        monkeypatch.setattr(eng, "_replay_neighbour", gated)
        out = await eng.read(
            "how many turns", namespace="a", mode="compose", top_k=1, replay_window=2
        )
        got = [r.record_id for r in out.context.records]
        assert hidden not in got and turns[5].record_id not in got  # strict: no bridging
        assert turns[2].record_id in got
    finally:
        await eng.stop()
