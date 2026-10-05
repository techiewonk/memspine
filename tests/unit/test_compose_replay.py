"""G2c: compose results replay their session neighbours (``read.compose_replay``)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.core.records import MemoryRecord

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _session(eng: Engine, monkeypatch: pytest.MonkeyPatch) -> list[MemoryRecord]:
    turns = []
    for i in range(7):
        turns.append(
            await eng.write(
                f"turn {i} of the talk",
                namespace="a",
                memory_type="episodic",
                valid_from=T0 + timedelta(minutes=i),
            )
        )
    hit = turns[3]

    async def fake_search(
        probe: str, namespace: str = "a", top_k: int = 8, **_: Any
    ) -> list[tuple[MemoryRecord, float]]:
        return [(hit, 0.9)]

    monkeypatch.setattr(eng, "_search", fake_search)  # compose reads via _search (A-1)
    return turns


async def test_compose_replays_the_neighbours_of_each_hit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(compose_replay=True)
    await eng.start()
    try:
        turns = await _session(eng, monkeypatch)
        out = await eng.read(
            "how many turns", namespace="a", mode="compose", top_k=1, replay_window=2
        )
        assert out.mode == "compose"
        got = [r.record_id for r in out.context.records]
        assert got == [t.record_id for t in turns[1:6]]  # the hit +-2, chronological
        assert out.context.tokens_used == sum(len(t.content) // 4 + 1 for t in turns[1:6])
    finally:
        await eng.stop()


async def test_off_keeps_the_hit_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine()
    await eng.start()
    try:
        turns = await _session(eng, monkeypatch)
        out = await eng.read("how many turns", namespace="a", mode="compose", top_k=1)
        assert [r.record_id for r in out.context.records] == [turns[3].record_id]
    finally:
        await eng.stop()


async def test_neighbours_stay_within_budget_nearest_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(compose_replay=True)
    await eng.start()
    try:
        turns = await _session(eng, monkeypatch)
        cost = len(turns[0].content) // 4 + 1
        out = await eng.read(
            "how many turns",
            namespace="a",
            mode="compose",
            top_k=1,
            replay_window=2,
            budget_tokens=3 * cost,
        )
        got = [r.record_id for r in out.context.records]
        assert got == [t.record_id for t in turns[2:5]]  # hit + the two nearest
        assert out.context.tokens_used <= 3 * cost
    finally:
        await eng.stop()


async def test_a_quarantined_neighbour_is_never_replayed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(compose_replay=True)
    await eng.start()
    try:
        turns = await _session(eng, monkeypatch)
        await eng.quarantine(turns[4].record_id, namespace="a")
        out = await eng.read(
            "how many turns", namespace="a", mode="compose", top_k=1, replay_window=1
        )
        got = [r.record_id for r in out.context.records]
        assert turns[4].record_id not in got
        assert turns[2].record_id in got and turns[3].record_id in got
    finally:
        await eng.stop()
