"""#28: multi-view fact fields (persons, location, topic) and their tags.

Fake miners and stub LLM providers only; no model is called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import MemspineConfig
from memspine.core.fact_views import normalise_view, tag_values, view_tags
from memspine.core.records import MemoryRecord
from memspine.prompts.models import ExtractedFact, ExtractedFacts
from memspine.prompts.registry import PromptRegistry
from memspine.services.llm.base import LLMRouter, LLMService

T0 = datetime(2023, 7, 15, 9, 0, tzinfo=UTC)
TURNS = [
    "Melanie: I went camping at Lake Tahoe with Caroline last Friday",
    "Caroline: so fun",
    "Melanie: we should go again",
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


async def _write_session(eng: Engine) -> None:
    msgs = [
        {"role": "user", "content": c, "timestamp": (T0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(TURNS)
    ]
    await eng.write_messages(msgs, namespace="a", session_id="s1", group_id="s1")


def _fact() -> ExtractedFact:
    return ExtractedFact(
        entity="Melanie",
        attribute="event",
        value="Melanie went camping with Caroline at Lake Tahoe on 2023-07-14",
        persons=["Melanie", "Caroline"],
        location="Lake  Tahoe",
        topic="Activities",
    )


async def _mined(eng: Engine) -> MemoryRecord:
    [fact] = [
        r
        for r in await eng.retrieve(namespace="a", memory_type="semantic")
        if "atomic_fact" in r.tags
    ]
    return fact


async def test_multiview_on_stores_view_tags_and_the_value_verbatim(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(mine_multiview=True)

    async def fake_mine(text: str) -> list[ExtractedFact]:
        return [_fact()]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        fact = await _mined(eng)
        assert fact.content == (
            "Melanie event: Melanie went camping with Caroline at Lake Tahoe on 2023-07-14"
        )
        assert tag_values(fact, "person:") == ["melanie", "caroline"]
        assert tag_values(fact, "loc:") == ["lake tahoe"]
        assert tag_values(fact, "topic:") == ["activities"]
    finally:
        await eng.stop()


async def test_multiview_off_stores_no_view_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine()

    async def fake_mine(text: str) -> list[ExtractedFact]:
        return [_fact()]

    monkeypatch.setattr(eng, "_build_fact_miner", lambda: fake_mine)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        fact = await _mined(eng)
        assert not any(t.startswith(("person:", "loc:", "topic:")) for t in fact.tags)
    finally:
        await eng.stop()


class _Extract:
    def __init__(self) -> None:
        self.systems: list[str] = []

    @property
    def provider_id(self) -> str:
        return "stub:extract"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        self.systems.append(messages[0]["content"])
        return (
            "facts:\n"
            "  - entity: Melanie\n"
            "    attribute: event\n"
            "    value: Melanie went camping at Lake Tahoe\n"
            "    kind: event\n"
            "    persons: Melanie, Caroline, unknown\n"
            "    location: N/A\n"
            "    topic: activities\n"
        )


@pytest.mark.parametrize(
    ("options", "variant"),
    [
        ({"mine_multiview": True}, "session4"),
        ({"mine_multiview": True, "mine_prompt": "session3"}, "session3"),
        ({"mine_prompt": "session4"}, "session4"),
        ({}, "session"),
    ],
)
async def test_miner_prompt_variant(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, Any], variant: str
) -> None:
    stub = _Extract()
    stubs: dict[str, LLMService] = {"extract": stub}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    eng = _engine(**options)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        [system] = stub.systems
        assert ("(9) views:" in system) is (variant == "session4")
        fact = await _mined(eng)
        if options.get("mine_multiview"):
            # Parsed through the guards: the placeholder person and location are gone.
            assert tag_values(fact, "person:") == ["melanie", "caroline"]
            assert tag_values(fact, "loc:") == []
            assert tag_values(fact, "topic:") == ["activities"]
        else:
            assert tag_values(fact, "person:") == []
    finally:
        await eng.stop()


def test_session4_prompt_is_a_new_variant() -> None:
    registry = PromptRegistry()
    v4 = registry.select("extract", condition="session4")
    assert (v4.id, v4.version, v4.token_budget) == ("extract@session4", 4, 4096)
    assert registry.select("extract", condition="session3").id == "extract@session3"
    system = v4.render({"content": "x"})[0]["content"]
    assert "`persons`" in system and "`location`" in system and "`topic`" in system


def test_view_fields_pass_the_guards_and_caps() -> None:
    long_name = "x" * 200
    raw = {
        "entity": "Melanie",
        "attribute": "event",
        "value": "v",
        "persons": ["Melanie", "melanie", "<person>", "Let me think", long_name, *"abcdefghij"],
        "location": "<think>somewhere",
        "topic": 42,
    }
    [fact] = ExtractedFacts.model_validate({"facts": [raw]}).facts
    assert fact.persons[0] == "Melanie" and len(fact.persons) == 8
    assert "melanie" not in fact.persons and "<person>" not in fact.persons
    assert all(len(p) <= 80 for p in fact.persons)
    assert fact.location is None and fact.topic == "42"
    # Missing views stay empty: the base miner output parses unchanged.
    [plain] = ExtractedFacts.model_validate(
        {"facts": [{"entity": "A", "attribute": "b", "value": "c"}]}
    ).facts
    assert plain.persons == [] and plain.location is None and plain.topic is None


def test_view_tags_normalise_and_dedupe() -> None:
    assert normalise_view("  Lake　TAHOE ") == "lake tahoe"
    assert view_tags(["Ana", "ANA", ""], "Paris", None) == ["person:ana", "loc:paris"]
    assert view_tags([], None, None) == []
