"""W19 (plan v3.2): ``read.raw_turn_floor``: derived records keep no raw-turn slot."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core import records as records_module

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)

TURNS = [
    "Melanie: I went camping with the kids last weekend",
    "Caroline: I started a new painting of a lake sunset",
    "Melanie: we ran a charity race for mental health in May",
    "Melanie: I went camping at the beach again with the family",
    "Caroline: I joined a support group last Tuesday",
    "Melanie: the kids loved the pottery class yesterday",
    "Caroline: camping is not for me, I prefer museums",
    "Melanie: we bought a new tent for camping trips",
]

#: Mined facts that echo the camping turns, so they outrank them in the search.
MINED = [
    ("Melanie event: went camping with the kids", 0),
    ("Melanie event: went camping at the beach with the family", 3),
    ("Melanie event: bought a new tent for camping", 7),
]

QUERY = "Where did Melanie go camping with the family?"


@pytest.fixture(autouse=True)
def _fixed_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    import uuid

    counter = iter(range(1, 1_000_000))
    monkeypatch.setattr(records_module, "uuid4", lambda: uuid.UUID(int=next(counter)))


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _raw_turns(*, mined: bool, mode: str, **read: Any) -> list[str]:
    eng = _engine(**read)
    await eng.start()
    try:
        ids = []
        for day, text in enumerate(TURNS):
            rec = await eng.write(
                text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(minutes=day)
            )
            ids.append(rec.record_id)
        if mined:
            for text, parent in MINED:
                await eng._deposit_mined_fact(
                    "a",
                    text,
                    "Melanie",
                    "event",
                    [ids[parent]],
                    T0 + timedelta(minutes=parent, seconds=30),
                    "s1",
                    kind="event",
                    persons=["Melanie"],
                )
        if mode == "assemble":
            records = (
                await eng.assemble(QUERY, namespace="a", top_k=2, budget_tokens=4000)
            ).records
        else:
            out = await eng.read(
                QUERY, namespace="a", mode=mode, top_k=2, budget_tokens=4000, replay_window=1
            )
            records = out.context.records
        return sorted(r.content for r in records if r.memory_type == "episodic")
    finally:
        await eng.stop()


def test_raw_turn_floor_defaults_off() -> None:
    assert ReadConfig().raw_turn_floor is False


@pytest.mark.parametrize("mode", ["retrieve", "assemble"])
async def test_derived_records_take_raw_turn_slots_when_off(mode: str) -> None:
    alone = await _raw_turns(mined=False, mode=mode)
    crowded = await _raw_turns(mined=True, mode=mode)
    assert len(crowded) < len(alone), "fixture must show the displacement"


@pytest.mark.parametrize("mode", ["replay", "retrieve", "assemble"])
async def test_raw_turn_floor_keeps_every_raw_turn_of_the_read_without_them(mode: str) -> None:
    alone = await _raw_turns(mined=False, mode=mode)
    floored = await _raw_turns(mined=True, mode=mode, raw_turn_floor=True)
    assert set(alone) <= set(floored)


@pytest.mark.parametrize("mode", ["replay", "retrieve", "assemble"])
async def test_raw_turn_floor_without_derived_records_is_byte_identical(mode: str) -> None:
    off = await _raw_turns(mined=False, mode=mode)
    on = await _raw_turns(mined=False, mode=mode, raw_turn_floor=True)
    assert on == off


def test_rerank_instruction_reaches_the_settings() -> None:
    """N12 (plan v3.2): the reranker instruction is configurable, default None."""
    from memspine.services.rerank.factory import RerankSettings

    assert ReadConfig().rerank_instruction is None
    assert RerankSettings(mode="qwen3", instruction="judge relevance").instruction == (
        "judge relevance"
    )
