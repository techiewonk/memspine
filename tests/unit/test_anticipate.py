"""H8: anticipatory cues from the sleep cycle (fake anticipator)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from memspine import Engine
from memspine.prompts.models import AnticipatedCue


def _engine(on: bool) -> Engine:
    policies = {"consolidation": {"anticipate": True}} if on else {}
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": policies},
            "semantic": {"enabled": True},
        },
        read={"anticipatory_cues": True, "hybrid": False},
    )


async def _session(eng: Engine) -> None:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    msgs = [
        {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(
            ["Alice: hi", "Alice: I just found out I am allergic to nuts", "Bob: oh no, take care"]
        )
    ]
    await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")


async def test_cues_land_on_the_answering_turn_once(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(on=True)
    seen: list[str] = []

    async def fake(text: str) -> list[AnticipatedCue]:
        seen.append(text)
        return [
            AnticipatedCue(line=2, cue="what snacks should we buy for Alice's party"),
            AnticipatedCue(line=99, cue="out of range is dropped"),
        ]

    monkeypatch.setattr(eng, "_build_anticipator", lambda: fake)
    await eng.start()
    try:
        await _session(eng)
        first = await eng.sleep()
        assert first["anticipate"]["cues"] == 1
        assert "[2] [2023-05-08] Alice: I just found out I am allergic to nuts" in seen[0]
        hits = await eng.search("snacks for Alice's party", namespace="a", top_k=1)
        assert "allergic to nuts" in hits[0][0].content  # the cue resolves to its turn
        again = await eng.sleep()
        assert again["anticipate"]["cues"] == 0  # idempotent
    finally:
        await eng.stop()


async def test_anticipate_off_by_default() -> None:
    eng = _engine(on=False)
    await eng.start()
    try:
        await _session(eng)
        assert (await eng.sleep())["anticipate"]["status"] == "skipped"
    finally:
        await eng.stop()
