"""Wave-1 read levers: #58 relative week, #29 card event dates, #35 planner v2 lookup
subqueries, #60 count dedupe. Hash embedder and stub providers only."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.lead import distinct_occurrences, event_day
from memspine.core.records import MemoryRecord
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)  # a Sunday


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


# -- #58 ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("week", "expected"),
    [
        (None, "Evan had a health scare last week [= 2023-05-29..2023-06-04]"),
        ("calendar", "Evan had a health scare last week [= 2023-05-29..2023-06-04]"),
        ("preceding_7_days", "Evan had a health scare last week [= 2023-05-30..2023-06-05]"),
    ],
)
async def test_relative_week_mode_in_the_assembled_context(week: str | None, expected: str) -> None:
    read: dict[str, Any] = {"resolve_relative_dates": True}
    if week is not None:
        read["relative_week"] = week
    eng = _engine(**read)
    await eng.start()
    try:
        rec = await eng.write(
            "Evan had a health scare last week",
            namespace="a",
            valid_from=datetime(2023, 6, 6, tzinfo=UTC),
        )
        ctx = await eng.assemble("health scare", namespace="a")
        [line] = [r.content for r in ctx.records if r.record_id == rec.record_id]
        assert line == expected
    finally:
        await eng.stop()


# -- #29: cards with happened dates -------------------------------------------------


async def _seed_cards(eng: Engine) -> None:
    turn = await eng.write(
        "Melanie: I went camping with the kids last Friday",
        namespace="a",
        memory_type="episodic",
        valid_from=T0 + timedelta(days=1),  # said Monday 2023-05-08
    )
    await eng._deposit_mined_fact(
        "a",
        "Melanie camping: Melanie went camping with her kids",
        "Melanie",
        "camping",
        [turn.record_id],
        datetime(2023, 5, 5, tzinfo=UTC),
        "s1",
        kind="event",
        happened="2023-05-05",
    )


@pytest.mark.parametrize("on", [False, True])
async def test_cards_event_date_renders_said_and_happened(on: bool) -> None:
    eng = _engine(cards="header", cards_event_date=on)
    await eng.start()
    try:
        await _seed_cards(eng)
        out = await eng.read("Melanie camping", namespace="a", mode="retrieve", budget_tokens=400)
        [header] = [r for r in out.context.records if constants.CARDS_TAG in r.tags]
        line = "Melanie: Melanie went camping with her kids"
        if on:
            assert f"[said 2023-05-08 · happened 2023-05-05] {line}" in header.content
        else:
            assert f"[said 2023-05-08] {line}" in header.content
            assert "happened" not in header.content
    finally:
        await eng.stop()


# -- #35: planner v2 lookup subqueries ------------------------------------------------


class _Plan:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.systems: list[str] = []

    @property
    def provider_id(self) -> str:
        return "stub:plan"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.systems.append(messages[0]["content"])
        return self.reply


LOOKUP = (
    "mode: lookup\ntemporal: false\nentities: [Ana]\n"
    "subqueries:\n  - kitten Miso\n  - Miso sleeps on the sofa"
)


def _planned(monkeypatch: pytest.MonkeyPatch, stub: _Plan, **read: Any) -> Engine:
    stubs: dict[str, LLMService] = {"plan": stub}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    return _engine(planner="llm", **read)


async def _seed_pets(eng: Engine) -> None:
    # The kitten line is the oldest, so recency never lifts it on its own.
    for i, text in enumerate(
        [
            "the kitten Miso sleeps on the sofa all day, says Ana",
            "Ana has a new bike for the summer",
            "Ana has a pet peeve about loud traffic",
        ]
    ):
        await eng.write(
            text,
            namespace="a",
            memory_type="semantic",  # no replay neighbours: only the ranking decides
            valid_from=T0 + timedelta(minutes=i),
        )


async def _read(eng: Engine) -> list[str]:
    out = await eng.read("Does Ana have a pet?", namespace="a", top_k=1, budget_tokens=25)
    return [r.content for r in out.context.records]


async def test_planner_v1_ignores_lookup_subqueries(monkeypatch: pytest.MonkeyPatch) -> None:
    stub = _Plan(LOOKUP)
    eng = _planned(monkeypatch, stub)
    await eng.start()
    try:
        await _seed_pets(eng)
        contents = await _read(eng)
        assert not any("kitten" in c for c in contents)
        assert "evidence itself would likely use" not in stub.systems[0]
    finally:
        await eng.stop()


@pytest.mark.parametrize("hybrid", [False, True])
async def test_planner_v2_fuses_lookup_subqueries(
    monkeypatch: pytest.MonkeyPatch, hybrid: bool
) -> None:
    stub = _Plan(LOOKUP)
    eng = _planned(monkeypatch, stub, planner_version="v2", hybrid=hybrid)
    await eng.start()
    try:
        await _seed_pets(eng)
        contents = await _read(eng)
        assert any("kitten Miso" in c for c in contents)
        assert "evidence itself would likely use" in stub.systems[0]  # plan@v2
        assert eng.model_calls() == {"plan": 1}
    finally:
        await eng.stop()


async def test_planner_v2_without_subqueries_matches_v1(monkeypatch: pytest.MonkeyPatch) -> None:
    reply = "mode: lookup\ntemporal: false\nentities: [Ana]\nsubqueries: []"
    outputs = []
    for version in ("v1", "v2"):
        eng = _planned(monkeypatch, _Plan(reply), planner_version=version)
        await eng.start()
        try:
            await _seed_pets(eng)
            outputs.append(await _read(eng))
        finally:
            await eng.stop()
    assert outputs[0] == outputs[1]


def test_plan_v2_prompt_is_a_variant() -> None:
    from memspine.prompts.registry import PromptRegistry

    registry = PromptRegistry()
    assert registry.select("plan").id == "plan"
    v2 = registry.select("plan", condition="v2")
    assert (v2.id, v2.version) == ("plan@v2", 2)
    system = v2.render({"query": "Is Caroline religious?"})[0]["content"]
    assert "For lookup and replay, write one or two short subqueries" in system
    assert "Caroline church" in system


# -- #60: count dedupe ----------------------------------------------------------------


def _mention(text: str, day: int, session: str) -> tuple[MemoryRecord, str]:
    rec = MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=text,
        valid_from=T0 + timedelta(days=day),
        group_id=session,
    )
    return rec, text


def test_count_dedupe_merges_one_event_said_on_two_days() -> None:
    mentions = [
        _mention("Melanie: we went to the beach today", 0, "s1"),
        _mention("Melanie: we went to the beach yesterday, so fun", 1, "s2"),
        _mention("Melanie: we went to the beach again today", 9, "s3"),
    ]
    assert len(distinct_occurrences(mentions)) == 3  # unchanged default
    days = {r.record_id: event_day(t, r.valid_from) for r, t in mentions}
    kept = distinct_occurrences(mentions, event_days=days)
    assert [t for _, t in kept] == [
        "Melanie: we went to the beach today",
        "Melanie: we went to the beach again today",
    ]


def test_count_dedupe_keeps_distinct_events_on_one_day() -> None:
    """Same event day but little word overlap: two occurrences."""
    mentions = [
        _mention("Melanie: we went to the beach today", 0, "s1"),
        _mention("Caroline: yesterday I painted a lake sunset in my studio", 1, "s2"),
    ]
    days = {r.record_id: event_day(t, r.valid_from) for r, t in mentions}
    assert len(distinct_occurrences(mentions, event_days=days)) == 2


async def test_count_dedupe_in_the_occurrences_block() -> None:
    query = "How many times has Melanie gone to the beach?"
    turns = [
        ("Melanie: we went to the beach today with the kids", 0, "s1"),
        ("Melanie: we went to the beach yesterday with the kids", 1, "s2"),
        ("Melanie: back at the beach again this morning", 9, "s3"),
    ]
    blocks = {}
    for dedupe in (False, True):
        eng = _engine(render="dated", count_timeline=True, count_dedupe=dedupe)
        await eng.start()
        try:
            for i, (text, day, session) in enumerate(turns):
                await eng.write(
                    text,
                    namespace="a",
                    memory_type="episodic",
                    valid_from=T0 + timedelta(days=day, minutes=i),
                    group_id=session,
                )
            out = await eng.read(query, namespace="a", mode="retrieve")
            [block] = [r for r in out.context.records if constants.COUNT_TAG in r.tags]
            blocks[dedupe] = block.content.splitlines()[1:]
        finally:
            await eng.stop()
    assert len(blocks[False]) == 3
    assert blocks[True] == [blocks[False][0], blocks[False][2]]
