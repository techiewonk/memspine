"""W10 (plan v3.2): ``read.session_cap``: at most N raw-turn hits per session."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)
#: Session 1 talks about camping four times; sessions 2 and 3 mention it once each.
SESSIONS = [
    [
        "Melanie: we went camping by the lake",
        "Melanie: camping with the kids was fun",
        "Melanie: the camping tent leaked a bit",
        "Melanie: next camping trip is in June",
    ],
    ["Melanie: we tried camping at the beach this time", "Caroline: I painted all day"],
    ["Melanie: I bought a camping stove", "Caroline: my support group met again"],
]
QUERY = "Where has Melanie gone camping?"


async def _days(**read: Any) -> list[int]:
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
        for day, turns in enumerate(SESSIONS):
            for minute, text in enumerate(turns):
                await eng.write(
                    text,
                    namespace="a",
                    memory_type="episodic",
                    valid_from=T0 + timedelta(days=7 * day, minutes=minute),
                )
        out = await eng.read(QUERY, namespace="a", mode="retrieve", top_k=4)
        return sorted((r.valid_from - T0).days // 7 for r in out.context.records)
    finally:
        await eng.stop()


def test_session_cap_defaults_off() -> None:
    assert ReadConfig().session_cap is None


async def test_session_cap_spreads_hits_over_sessions() -> None:
    capped = await _days(session_cap=1)
    assert sorted(capped) == [0, 1, 2]  # one hit per session, every session reached


async def test_session_cap_two_never_exceeds_two_per_session() -> None:
    capped = await _days(session_cap=2)
    assert len(capped) == len(await _days())
    assert all(capped.count(s) <= 2 for s in set(capped))
