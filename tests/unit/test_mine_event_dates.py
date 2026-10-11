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
    assert (v3.id, v3.version, v3.token_budget) == ("extract@session3", 4, 4096)
    v2 = registry.select("extract", condition="session")
    assert v2.id == "extract@session" and (v2.token_budget or 0) < 4096
    system = v3.render({"content": "x"})[0]["content"]
    assert "2021-03-12" in system and "complete coverage" in system and "`turns`" in system


# -- review fixes: one date shift, plans stay plans, the right anchor turn ----------


async def _mine_turns(
    monkeypatch: pytest.MonkeyPatch,
    turns: list[tuple[datetime, str]],
    mined: list[ExtractedFact],
) -> tuple[Engine, dict[str, Any]]:
    eng = _engine(mine_evidence_turns=True, mine_event_dates=True)

    async def fake_mine(text: str) -> list[ExtractedFact]:
        return mined

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    last = turns[-1][0]
    filler = [(last + timedelta(minutes=n), f"Caro: ok {n}") for n in range(1, 3)]
    msgs = [
        {"role": "user", "content": c, "timestamp": t.isoformat()} for t, c in [*turns, *filler]
    ]
    await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")
    await eng.sleep()
    return eng, await _facts(eng)


async def test_happened_fact_is_not_shifted_twice_at_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A fact keeping its relative phrase is resolved against the day it was SAID, not
    against a ``valid_from`` already moved to the event day."""
    from memspine.core.lead import event_day

    turns = [(T0, "Melanie: I went camping yesterday, and hiked last week")]
    mined = [
        ExtractedFact(
            entity="Mel", attribute="camp", value="Mel went camping yesterday", turns=[1]
        ),
        ExtractedFact(entity="Mel", attribute="hike", value="Mel hiked last week", turns=[1]),
    ]
    eng, facts = await _mine_turns(monkeypatch, turns, mined)
    try:
        camp, hike = facts["Mel camp"], facts["Mel hike"]
        assert camp.valid_from == datetime(2023, 5, 7, tzinfo=UTC)
        assert {"happened:2023-05-07", "said:2023-05-08"} <= set(camp.tags)
        assert "yesterday [= Sun 2023-05-07]" in eng._annotate_dates(camp).content
        assert hike.valid_from == datetime(2023, 5, 1, tzinfo=UTC)
        assert "last week [= 2023-05-01..2023-05-07]" in eng._annotate_dates(hike).content
        assert event_day(camp.content, camp.valid_from, record=camp).isoformat() == "2023-05-07"
    finally:
        await eng.stop()


def test_legacy_happened_fact_without_said_tag_is_not_resolved() -> None:
    from memspine.core.event_date import date_anchor
    from memspine.core.lead import event_day
    from memspine.core.records import MemoryRecord

    moved = MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content="Mel went camping yesterday",
        valid_from=datetime(2023, 5, 7, tzinfo=UTC),
        tags=["atomic_fact", "happened:2023-05-07"],
    )
    assert date_anchor(moved) is None
    assert event_day(moved.content, moved.valid_from, record=moved).isoformat() == "2023-05-07"
    plain = moved.model_copy(update={"tags": ["atomic_fact"]})
    assert date_anchor(plain) == plain.valid_from


async def test_future_plan_keeps_said_valid_from(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan ("next month", "next year") keeps its happened label but never becomes a
    future ``valid_from`` (the conflict policy keeps the newest one)."""
    turns = [(T0, "Mel: I will start a new job next month and move next year")]
    mined = [
        ExtractedFact(entity="Mel", attribute="job", value="Mel will start a job next month"),
        ExtractedFact(entity="Mel", attribute="move", value="Mel will move next year"),
        # The miner's own date, in the future but inside the slack: also clamped.
        ExtractedFact(entity="Mel", attribute="trip", value="Mel plans a trip", date="2023-09-01"),
    ]
    eng, facts = await _mine_turns(monkeypatch, turns, mined)
    try:
        job, move, trip = facts["Mel job"], facts["Mel move"], facts["Mel trip"]
        assert "happened:2023-06" in job.tags and job.valid_from == T0
        assert "happened:2024" in move.tags and move.valid_from == T0
        assert "next year [= 2024]" in eng._annotate_dates(move).content
        assert "happened:2023-09-01" in trip.tags and trip.valid_from == T0
    finally:
        await eng.stop()


async def test_fact_resolves_against_the_turn_holding_its_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cited turns on different days: the phrase is resolved against the turn it is in."""
    day2 = T0 + timedelta(days=3)  # Thursday 2023-05-11
    turns = [
        (T0, "Mel: I love camping"),
        (day2, "Mel: we went camping yesterday"),
    ]
    mined = [
        ExtractedFact(entity="Mel", attribute="camp", value="Mel camped yesterday", turns=[1, 2]),
        ExtractedFact(entity="Mel", attribute="love", value="Mel loves camping", turns=[1, 3]),
    ]
    eng, facts = await _mine_turns(monkeypatch, turns, mined)
    try:
        camp = facts["Mel camp"]
        assert "happened:2023-05-10" in camp.tags
        assert camp.valid_from == datetime(2023, 5, 10, tzinfo=UTC)
        assert "yesterday [= Wed 2023-05-10]" in eng._annotate_dates(camp).content
    finally:
        await eng.stop()


async def test_fill_dates_ceiling_is_the_sessions_last_turn() -> None:
    from memspine.workers.pipelines import _fill_dates, _Happened

    async def dater(transcript: str, facts: list[str]) -> dict[int, str]:
        return {1: "2023-05-01", 2: "2025-01-01"}  # the second is far past the session

    mined = [ExtractedFact(entity="a", attribute="b", value=v) for v in ("x", "y")]
    happened = [_Happened(T0), _Happened(T0)]
    await _fill_dates(dater, "", mined, happened, T0)
    assert [h.label for h in happened] == ["2023-05-01", None]
