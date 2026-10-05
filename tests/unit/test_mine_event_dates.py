"""#29 / #27: happened dates, evidence-turn parents and the session3 miner prompt.

Fake miners and fake LLM providers only; no model is called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.prompts.models import ExtractedFact
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)  # a Monday
TURNS = [
    "Melanie: I went camping with the kids last Friday",
    "Caroline: that is great, how was it",
    "Melanie: lovely, and I signed up for a pottery class",
]


def _engine(**consolidation: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"consolidation": {"mine_facts": True, **consolidation}},
            },
            "semantic": {"enabled": True},
        },
    )


async def _write_session(eng: Engine) -> list[str]:
    msgs = [
        {"role": "user", "content": c, "timestamp": (T0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(TURNS)
    ]
    records = await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
    return [r.record_id for r in records]


async def _facts(eng: Engine) -> dict[str, Any]:
    return {
        r.content.split(":")[0]: r
        for r in await eng.retrieve(namespace="a", memory_type="semantic")
        if "atomic_fact" in r.tags
    }


def _mined() -> list[ExtractedFact]:
    return [
        # The miner left the relative phrase in: the rules resolve it (Fri 2023-05-05).
        ExtractedFact(
            entity="Melanie",
            attribute="camping",
            value="Melanie went camping with her kids last Friday",
            turns=[1, 2],
        ),
        # Cited, dated by the miner, no phrase: the miner's date is the happened date.
        ExtractedFact(
            entity="Melanie",
            attribute="pottery",
            value="Melanie signed up for a pottery class",
            date="2023-05-08",
            turns=[3],
        ),
        # Uncited and undated: no happened date; parents stay the whole session.
        ExtractedFact(entity="Caroline", attribute="mood", value="Caroline was supportive"),
    ]


async def test_event_dates_and_cited_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(mine_evidence_turns=True, mine_event_dates=True)
    seen: list[str] = []

    async def fake_mine(text: str) -> list[ExtractedFact]:
        seen.append(text)
        return _mined()

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        ids = await _write_session(eng)
        stats = await eng.sleep()
        assert stats["mine_facts"]["facts"] == 3
        assert seen[0].splitlines()[0] == f"[1] [2023-05-08] {TURNS[0]}"
        assert seen[0].splitlines()[2] == f"[3] [2023-05-08] {TURNS[2]}"
        facts = await _facts(eng)
        camping = facts["Melanie camping"]
        assert "happened:2023-05-05" in camping.tags
        assert camping.valid_from == datetime(2023, 5, 5, tzinfo=UTC)  # rule-resolved
        assert camping.source.parents == ids[:2]
        pottery = facts["Melanie pottery"]
        assert "happened:2023-05-08" in pottery.tags
        assert pottery.source.parents == [ids[2]]
        mood = facts["Caroline mood"]
        assert not any(t.startswith("happened:") for t in mood.tags)
        assert mood.source.parents == ids
        assert mood.valid_from == T0
    finally:
        await eng.stop()


async def test_options_off_keep_transcript_parents_and_tags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Default options: un-numbered transcript, whole-session parents, no happened tag,
    and the miner's ``turns`` are ignored."""
    eng = _engine()
    seen: list[str] = []

    async def fake_mine(text: str) -> list[ExtractedFact]:
        seen.append(text)
        return _mined()

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        ids = await _write_session(eng)
        await eng.sleep()
        assert seen[0].splitlines()[0] == f"[2023-05-08] {TURNS[0]}"
        facts = await _facts(eng)
        assert all(f.source.parents == ids for f in facts.values())
        assert not any(t.startswith("happened:") for f in facts.values() for t in f.tags)
        assert facts["Melanie camping"].valid_from == T0
    finally:
        await eng.stop()


async def test_relative_week_applies_to_mined_dates(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"relative_week": "preceding_7_days"},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {"consolidation": {"mine_facts": True, "mine_event_dates": True}},
            },
            "semantic": {"enabled": True},
        },
    )

    async def fake_mine(text: str) -> list[ExtractedFact]:
        return [ExtractedFact(entity="Melanie", attribute="trip", value="Melanie hiked last week")]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        fact = (await _facts(eng))["Melanie trip"]
        assert "happened:2023-05-01..2023-05-07" in fact.tags
        assert fact.valid_from == datetime(2023, 5, 1, tzinfo=UTC)
    finally:
        await eng.stop()


# -- the batched LLM fill (fake provider through a real router) ------------------------


class _Extract:
    """A stub ``extract`` provider: replies to the miner and to the date fill."""

    def __init__(self, dates_reply: str | Exception) -> None:
        self.dates_reply = dates_reply
        self.calls: list[list[dict[str, str]]] = []

    @property
    def provider_id(self) -> str:
        return "stub:extract"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.calls.append(messages)
        if "You date facts" in messages[0]["content"]:
            if isinstance(self.dates_reply, Exception):
                raise self.dates_reply
            return self.dates_reply
        return (
            "facts:\n"
            "  - {entity: Melanie, attribute: camping, value: Melanie went camping last Friday}\n"
            "  - {entity: Melanie, attribute: pottery, value: Melanie joined a pottery class}\n"
            "  - {entity: Caroline, attribute: mood, value: Caroline was supportive}\n"
        )


def _llm_engine(monkeypatch: pytest.MonkeyPatch, stub: _Extract, **options: Any) -> Engine:
    stubs: dict[str, LLMService] = {"extract": stub}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    return _engine(mine_event_dates=True, **options)


async def test_llm_fill_dates_only_the_undated_facts(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Extract("dates:\n  - {index: 1, date: 2023-04-30}\n  - {index: 2, date: ''}\n")
    eng = _llm_engine(monkeypatch, stub, mine_event_dates_llm=True)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        [_, dating] = stub.calls
        user = dating[-1]["content"]
        # Only the two facts no rule dated, numbered in the batch.
        assert "[1] Melanie joined a pottery class" in user
        assert "[2] Caroline was supportive" in user
        assert "camping" not in user.split("facts:", 1)[1]
        facts = await _facts(eng)
        assert "happened:2023-05-05" in facts["Melanie camping"].tags  # rules, not the LLM
        assert "happened:2023-04-30" in facts["Melanie pottery"].tags  # the LLM fill
        assert facts["Melanie pottery"].valid_from == T0  # an LLM date is a tag only
        assert not any(t.startswith("happened:") for t in facts["Caroline mood"].tags)
    finally:
        await eng.stop()


async def test_llm_fill_failure_leaves_facts_undated(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Extract(RuntimeError("provider down"))
    eng = _llm_engine(monkeypatch, stub, mine_event_dates_llm=True)
    await eng.start()
    try:
        await _write_session(eng)
        stats = await eng.sleep()
        assert stats["mine_facts"]["facts"] == 3
        facts = await _facts(eng)
        assert "happened:2023-05-05" in facts["Melanie camping"].tags
        assert not any(t.startswith("happened:") for t in facts["Melanie pottery"].tags)
    finally:
        await eng.stop()


async def test_llm_fill_off_makes_no_dating_call(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Extract("dates: []\n")
    eng = _llm_engine(monkeypatch, stub)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        assert len(stub.calls) == 1  # the miner only
        assert eng.model_calls() == {"extract": 1}
    finally:
        await eng.stop()


# -- #27: extract@session3 ----------------------------------------------------------


async def test_mine_prompt_session3_selects_v3_and_caps_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[tuple[str, dict[str, Any]]] = []

    class _Rec(_Extract):
        async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
            seen.append((messages[0]["content"], options))
            return "facts: []\n"

    for variant, marker, cap in (
        ("session", "(6) classify each fact", None),
        ("session3", "complete coverage", 4096),
    ):
        seen.clear()
        eng = _llm_engine(monkeypatch, _Rec("dates: []"), mine_prompt=variant)
        await eng.start()
        try:
            await _write_session(eng)
            await eng.sleep()
            [(system, options)] = seen
            assert marker in system
            assert options.get("max_tokens") == cap
            assert ("worked" in system or "Example." in system) is (variant == "session3")
        finally:
            await eng.stop()


def test_session3_prompt_is_v3_and_a_variant() -> None:
    from memspine.prompts.registry import PromptRegistry

    registry = PromptRegistry()
    v3 = registry.select("extract", condition="session3")
    assert (v3.id, v3.version, v3.token_budget) == ("extract@session3", 3, 4096)
    v2 = registry.select("extract", condition="session")
    assert v2.id == "extract@session" and (v2.token_budget or 0) < 4096
    system = v3.render({"content": "x"})[0]["content"]
    assert "2023-07-14" in system and "complete coverage" in system and "`turns`" in system
