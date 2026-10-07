"""N01 (plan v3.2): ``read.present_order``: recorded-order presentation."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig

T0 = datetime(2023, 1, 1, tzinfo=UTC)
#: (text, entity, attribute, day): two values of Jon's job, plus an unrelated note.
ROWS = [
    ("Jon works as a banker at the city branch", "Jon", "job", 0),
    ("Jon now works as a dance teacher at his studio", "Jon", "job", 40),
    ("Jon likes jazz and swing music", "Jon", "music", 10),
]
QUERY = "What does Jon work as now, banker or dance teacher?"


async def _contents(keyed: bool = True, **read: Any) -> list[str]:
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
        for text, entity, attribute, day in ROWS:
            await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                valid_from=T0 + timedelta(days=day),
                entity=entity if keyed else None,
                attribute=attribute if keyed else None,
            )
        out = await eng.read(QUERY, namespace="a", mode="retrieve", top_k=3)
        return [r.content for r in out.context.records]
    finally:
        await eng.stop()


def _chrono(contents: list[str]) -> list[str]:
    day = {text: d for text, _, _, d in ROWS}
    return sorted(contents, key=day.__getitem__)


def test_present_order_defaults_to_relevance() -> None:
    assert ReadConfig().present_order == "relevance"


async def test_recorded_order_puts_records_in_time_order() -> None:
    out = await _contents(present_order="recorded")
    assert out == _chrono(out)
    assert sorted(out) == sorted(await _contents())


async def test_recorded_if_shared_key_orders_only_when_a_key_repeats() -> None:
    shared = await _contents(present_order="recorded_if_shared_key")
    assert shared == _chrono(shared)
    unkeyed = await _contents(keyed=False, present_order="recorded_if_shared_key")
    assert unkeyed == await _contents(keyed=False)
