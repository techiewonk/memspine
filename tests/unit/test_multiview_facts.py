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
from memspine.prompts.models import ExtractedFact, ExtractedFacts
from memspine.prompts.registry import PromptRegistry
from memspine.services.llm.base import LLMRouter, LLMService
from memspine.store.records import MemoryRecord

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
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}},
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
    await eng.ingest_messages(msgs, namespace="a", session_id="s1", group_id="s1")


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


@pytest.mark.parametrize("options", [{"mine_multiview": True}, {}])
async def test_base_miner_prompt_supports_optional_multiview(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
) -> None:
    stub = _Extract()
    stubs: dict[str, LLMService] = {"extract": stub}

    async def router(
        config: MemspineConfig, hooks: object = None, wiring: object = None
    ) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr("memspine.runtime.container.build_llm_router", router)
    eng = _engine(**options)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        [system] = stub.systems
        assert "(9) views:" in system
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


def test_base_session_prompt_contains_current_view_fields() -> None:
    prompt = PromptRegistry().select("extract", condition="session")
    assert prompt.id == "extract@session" and prompt.token_budget == 4096
    system = prompt.render({"content": "x"})[0]["content"]
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
