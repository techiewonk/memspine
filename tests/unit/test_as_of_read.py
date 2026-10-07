"""W7 (plan v3.2): ``as_of`` reads memory as it stood at a point in time."""

from __future__ import annotations

from datetime import UTC, datetime

from memspine import Engine

JAN = datetime(2023, 1, 10, tzinfo=UTC)
JUN = datetime(2023, 6, 10, tzinfo=UTC)


async def _engine() -> Engine:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False},
    )
    await eng.start()
    await eng.write(
        "Alice lives in Berlin",
        namespace="a",
        memory_type="semantic",
        entity="alice",
        attribute="city",
        valid_from=JAN,
    )
    await eng.write(
        "Alice lives in Munich",
        namespace="a",
        memory_type="semantic",
        entity="alice",
        attribute="city",
        valid_from=JUN,
    )
    return eng


async def test_default_read_sees_the_current_fact_only() -> None:
    eng = await _engine()
    try:
        out = await eng.read("Where does Alice live?", namespace="a", mode="retrieve")
        contents = [r.content for r in out.context.records]
    finally:
        await eng.stop()
    assert any("Munich" in c for c in contents)
    assert not any("Berlin" in c for c in contents)


async def test_as_of_read_sees_the_fact_current_then() -> None:
    eng = await _engine()
    try:
        march = datetime(2023, 3, 1, tzinfo=UTC)
        out = await eng.read("Where does Alice live?", namespace="a", mode="retrieve", as_of=march)
        ctx = await eng.assemble("Where does Alice live?", namespace="a", as_of=march)
    finally:
        await eng.stop()
    for contents in ([r.content for r in out.context.records], [r.content for r in ctx.records]):
        assert any("Berlin" in c for c in contents)
        assert not any("Munich" in c for c in contents)
