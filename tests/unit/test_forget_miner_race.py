"""I72: a hard forget racing the sleep-cycle miner must not leave a derived fact."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from memspine import Engine
from memspine.prompts.models import ExtractedFact

SECRET = "Caroline: my passport number is X4471920"


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": {"consolidation": {"mine_facts": True}}},
            "semantic": {"enabled": True},
        },
    )


async def _session(eng: Engine) -> list[str]:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    lines = [SECRET, "Melanie: noted", "Caroline: thanks"]
    msgs = [
        {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(lines)
    ]
    await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
    turns = await eng.retrieve(namespace="a", memory_type="episodic")
    return [t.record_id for t in turns if "passport" in t.content]


def _fact() -> ExtractedFact:
    return ExtractedFact(entity="caroline", attribute="passport", value="X4471920")


async def _derived(eng: Engine) -> list[str]:
    storage = eng._require_started()
    return [
        r.content for r in await storage.list_records("a", "semantic") if "X4471920" in r.content
    ]


async def test_forget_during_mining_leaves_no_derived_fact(monkeypatch) -> None:
    eng = _engine()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking_mine(text: str) -> list[ExtractedFact]:
        entered.set()
        await release.wait()
        return [_fact()]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: blocking_mine)
    await eng.start()
    try:
        [turn_id] = await _session(eng)
        sleeper = asyncio.create_task(eng.sleep())
        await asyncio.wait_for(entered.wait(), 10)
        await eng.forget(turn_id, namespace="a", hard=True)
        release.set()
        stats = await sleeper
        assert await _derived(eng) == []
        assert (await eng.verify_forget(turn_id, namespace="a"))["clean"] is True
        assert stats["mine_facts"]["errors"] == []
        # replay: the erased turn is never re-mined, and the session is not half-done
        again = await eng.sleep()
        assert again["mine_facts"]["facts"] == 0
        assert await _derived(eng) == []
    finally:
        await eng.stop()


async def test_forget_after_deposit_cascades_to_the_mined_fact(monkeypatch) -> None:
    eng = _engine()

    async def mine(text: str) -> list[ExtractedFact]:
        return [_fact()]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: mine)
    await eng.start()
    try:
        [turn_id] = await _session(eng)
        await eng.sleep()
        assert len(await _derived(eng)) >= 1
        await eng.forget(turn_id, namespace="a", hard=True)
        assert await _derived(eng) == []
        assert (await eng.verify_forget(turn_id, namespace="a"))["clean"] is True
    finally:
        await eng.stop()


async def test_forget_during_summarising_leaves_no_summary(monkeypatch) -> None:
    eng = _engine()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking_summarize(text: str) -> str:
        entered.set()
        await release.wait()
        return "Summary: " + text

    await eng.start()
    try:
        eng._summarize = blocking_summarize
        [turn_id] = await _session(eng)
        sleeper = asyncio.create_task(eng.sleep())
        await asyncio.wait_for(entered.wait(), 10)
        await eng.forget(turn_id, namespace="a", hard=True)
        release.set()
        await sleeper
        assert await _derived(eng) == []
        assert (await eng.verify_forget(turn_id, namespace="a"))["clean"] is True
    finally:
        await eng.stop()
