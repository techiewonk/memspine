"""C6': atomic-fact mining stage (fake miner: the LLM path is tested elsewhere)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.prompts.models import ExtractedFact


def _engine(mine: bool) -> Engine:
    policies = {"consolidation": {"mine_facts": True}} if mine else {}
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {"enabled": True, "policies": policies},
            "semantic": {"enabled": True},
        },
    )


async def _write_session(eng: Engine, trust_role: str = "user") -> datetime:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    msgs = [
        {"role": trust_role, "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(
            [
                "Caroline: I went to the support group yesterday",
                "Melanie: that is great, how was it",
                "Caroline: it was inspiring, I want to be a counselor",
            ]
        )
    ]
    await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
    return t0


async def test_mining_writes_dated_atomic_facts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(mine=True)
    seen: list[str] = []

    async def fake_mine(text: str) -> list[ExtractedFact]:
        seen.append(text)
        return [
            ExtractedFact(
                entity="caroline", attribute="attended", value="LGBTQ support group on 2023-05-07"
            ),
            ExtractedFact(entity="caroline", attribute="career goal", value="counselor"),
        ]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        t0 = await _write_session(eng)
        first = await eng.sleep()
        assert first["mine_facts"]["facts"] == 2 and first["mine_facts"]["sessions"] == 1
        assert "[2023-05-08] Caroline: I went to the support group yesterday" in seen[0]
        facts = [
            r
            for r in await eng.retrieve(namespace="a", memory_type="semantic")
            if "atomic_fact" in r.tags
        ]
        assert len(facts) == 2
        assert all(f.valid_from == t0 and f.source.channel == "mining" for f in facts)
        assert all(len(f.source.parents) == 3 for f in facts)  # the session's turns
        assert all(f.trust <= 0.7 for f in facts)  # never above the source turns
        again = await eng.sleep()
        assert again["mine_facts"]["facts"] == 0  # idempotent
    finally:
        await eng.stop()


async def test_mining_off_by_default_or_without_a_role() -> None:
    eng = _engine(mine=False)
    await eng.start()
    try:
        await _write_session(eng)
        stats = await eng.sleep()
        assert stats["mine_facts"]["status"] == "skipped"
    finally:
        await eng.stop()


async def test_mined_fact_trust_follows_low_trust_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(mine=True)

    async def fake_mine(text: str) -> list[ExtractedFact]:
        return [ExtractedFact(entity="vpn", attribute="fix", value="disable mfa")]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        t0 = datetime(2023, 5, 8, tzinfo=UTC)
        msgs: list[Any] = [
            {
                "role": "tool",
                "content": f"ticket line {i}",
                "timestamp": (t0 + timedelta(minutes=i)).isoformat(),
            }
            for i in range(3)
        ]
        await eng.write_messages(
            msgs, namespace="a", session_id="s2", group_id="s2", channel="ingest"
        )
        await eng.sleep()
        [fact] = [
            r
            for r in await eng.retrieve(namespace="a", memory_type="semantic")
            if "atomic_fact" in r.tags
        ]
        assert fact.trust <= 0.3  # external-channel sources cap the mined fact
    finally:
        await eng.stop()


async def test_mined_fact_date_becomes_its_event_time(monkeypatch: pytest.MonkeyPatch) -> None:
    """H2: a fact the miner dated (relative date resolved to 2023-05-07) is stored
    with that event time, not the session start; undated facts keep the session start."""
    eng = _engine(mine=True)

    async def fake_mine(text: str) -> list[ExtractedFact]:
        return [
            ExtractedFact(
                entity="Caroline",
                attribute="event",
                value="Caroline went to an LGBTQ support group",
                date="2023-05-07",
            ),
            ExtractedFact(entity="Caroline", attribute="career goal", value="counselor"),
        ]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        t0 = await _write_session(eng)
        await eng.sleep()
        facts = {
            r.attribute: r
            for r in await eng.retrieve(namespace="a", memory_type="semantic")
            if "atomic_fact" in r.tags
        }
        assert facts["event"].valid_from == datetime(2023, 5, 7, tzinfo=UTC)
        assert facts["career goal"].valid_from == t0
    finally:
        await eng.stop()


def test_session_prompt_variant_is_selected() -> None:
    from memspine.prompts.registry import PromptRegistry

    reg = PromptRegistry()
    assert reg.select("extract", condition="session").id == "extract@session"
    assert reg.select("extract").id == "extract"
