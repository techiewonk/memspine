"""H14: profile insights from the sleep cycle (fake reflector)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine


def _engine(on: bool) -> Engine:
    policies = {"consolidation": {"reflect_profile": True}} if on else {}
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": policies},
            "reflective": {"enabled": True},
        },
    )


async def _session(eng: Engine) -> None:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    msgs = [
        {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(
            [
                "Ana: I run every morning before work",
                "Ana: and I never drink coffee after noon",
                "Bob: nice routine",
            ]
        )
    ]
    await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")


async def test_insights_are_stored_once_with_evidence(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(on=True)

    async def fake(episodes: list[str]) -> list[tuple[str, list[int]]]:
        assert episodes[0].startswith("[2023-05-08] ")
        return [("Ana keeps a strict morning routine", [0, 1]), ("ignored", [99])]

    monkeypatch.setattr(eng, "_build_reflector", lambda: fake)
    await eng.start()
    try:
        await _session(eng)
        first = await eng.sleep()
        assert first["reflect_profile"]["insights"] == 1
        refl = await eng.retrieve(namespace="a", memory_type="reflective")
        assert [r.content for r in refl] == ["Ana keeps a strict morning routine"]
        events = await eng._require_started().read_events(after_seq=0, limit=1000)
        members = [
            e.payload["reflection"]["member_record_ids"]
            for e in events
            if "reflection" in (e.payload or {})
        ]
        assert members and len(members[0]) == 2  # evidence lineage is in the log
        again = await eng.sleep()
        assert again["reflect_profile"]["insights"] == 0  # idempotent
    finally:
        await eng.stop()


async def test_reflect_profile_off_by_default() -> None:
    eng = _engine(on=False)
    await eng.start()
    try:
        await _session(eng)
        assert (await eng.sleep())["reflect_profile"]["status"] == "skipped"
    finally:
        await eng.stop()
