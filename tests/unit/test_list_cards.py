"""#30: person-level list cards over mined event facts (``consolidation.list_cards``).

Fake miners and stub LLM providers only; no model is called.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import MemspineConfig
from memspine.core.records import MemoryRecord, RecordStatus, SourceInfo
from memspine.prompts.models import ExtractedFact
from memspine.services.llm.base import LLMRouter, LLMService
from memspine.workers.list_cards import (
    class_key,
    fact_class,
    fact_persons,
    render_list_card,
)

T0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
TURNS = [
    "Melanie: I did pottery, went camping, painted, swam and ran a 5k this year",
    "Caroline: wow, that is a lot",
    "Melanie: and Max the dog loves the beach",
]
ACTIVITIES = [
    ("pottery", "Melanie took a pottery class", "2023-05-02"),
    ("camping", "Melanie went camping with her kids", "2023-07-14"),
    ("painting", "Melanie painted a sunrise", "2023-06-01"),
    ("swimming", "Melanie went swimming at the lake", "2023-08-03"),
    ("running", "Melanie ran a charity 5k race", "2023-09-10"),
]


def _activities(**extra: Any) -> list[ExtractedFact]:
    return [
        ExtractedFact(entity="Melanie", attribute="activity", value=value, date=day, **extra)
        for _, value, day in ACTIVITIES
    ]


def _engine(read: dict[str, Any] | None = None, **consolidation: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False, "record_access": False, **(read or {})},
        memories={
            "episodic": {
                "enabled": True,
                "policies": {
                    "consolidation": {"mine_facts": True, "list_cards": True, **consolidation}
                },
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


async def _semantic(eng: Engine) -> list[MemoryRecord]:
    storage = eng._require_started()
    return await storage.list_records("a", "semantic")


def _live_cards(records: list[MemoryRecord]) -> list[MemoryRecord]:
    return [
        r
        for r in records
        if constants.LIST_CARD_TAG in r.tags and r.status is RecordStatus.ACTIVATED
    ]


def _mine(facts: list[ExtractedFact]) -> Any:
    async def fake_mine(text: str) -> list[ExtractedFact]:
        return list(facts)

    return lambda: fake_mine


async def test_five_activity_events_make_one_card_listing_all_five(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine()
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(_activities()))
    await eng.start()
    try:
        await _write_session(eng)
        stats = await eng.sleep()
        assert stats["mine_facts"]["list_cards"]["cards"] == 1
        records = await _semantic(eng)
        [card] = _live_cards(records)
        assert card.content == (
            "Melanie — activity: Melanie took a pottery class (2023-05), Melanie painted a "
            "sunrise (2023-06), Melanie went camping with her kids (2023-07), Melanie went "
            "swimming at the lake (2023-08), Melanie ran a charity 5k race (2023-09)"
        )
        facts = [r for r in records if "kind:event" in r.tags]
        assert sorted(card.source.parents) == sorted(f.record_id for f in facts)
        assert card.trust <= min(f.trust for f in facts)
        assert card.entity == "Melanie" and card.attribute is None
        assert card.source.role == "assistant" and card.source.channel == "list_card"
        # Unchanged membership: the next cycle writes nothing.
        again = await eng.sleep()
        assert again["mine_facts"]["list_cards"] == {
            "status": "ok",
            "cards": 0,
            "kept": 1,
            "archived": 0,
            "errors": [],
        }
        assert _live_cards(await _semantic(eng)) == [card]
    finally:
        await eng.stop()


@pytest.mark.parametrize("hard", [False, True])
async def test_forgetting_one_event_rederives_the_card_without_it(
    monkeypatch: pytest.MonkeyPatch, hard: bool
) -> None:
    eng = _engine()
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(_activities()))
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        [card] = _live_cards(await _semantic(eng))
        camping = next(
            r for r in await _semantic(eng) if "camping" in r.content and "kind:event" in r.tags
        )
        await eng.forget(camping.record_id, namespace="a", hard=hard)
        await eng.sleep()
        records = await _semantic(eng)
        [fresh] = _live_cards(records)
        assert fresh.record_id != card.record_id
        assert "camping" not in fresh.content and fresh.content.count("(2023-") == 4
        assert camping.record_id not in fresh.source.parents
        storage = eng._require_started()
        old = await storage.get_record(card.record_id)
        # A soft forget leaves the card to be superseded; a hard one cascades to it.
        assert old is None or old.status is not RecordStatus.ACTIVATED
    finally:
        await eng.stop()


async def test_a_new_event_rederives_and_archives_the_old_card(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine()
    first = _activities()[:3]
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(first))
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        [card] = _live_cards(await _semantic(eng))
        assert card.content.count("(2023-") == 3
        # A new event fact (deposited as a later session's miner would) joins the group.
        parents = [r.record_id for r in await eng.retrieve(namespace="a", memory_type="episodic")]
        await eng._deposit_mined_fact(
            "a",
            "Melanie activity: Melanie went swimming at the lake",
            "Melanie",
            "activity",
            parents,
            datetime(2023, 8, 3, tzinfo=UTC),
            "s2",
            kind="event",
        )
        stats = await eng.sleep()
        assert stats["mine_facts"]["list_cards"]["archived"] == 1
        [fresh] = _live_cards(await _semantic(eng))
        assert "swimming" in fresh.content and fresh.content.count("(2023-") == 4
        old = await eng._require_started().get_record(card.record_id)
        assert old is not None and old.status is RecordStatus.ARCHIVED
    finally:
        await eng.stop()


async def test_group_below_the_floor_gets_no_card_and_state_facts_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    facts = [
        ExtractedFact(entity="Melanie", attribute="activity", value="Melanie did pottery"),
        ExtractedFact(
            entity="Melanie", attribute="city", value="Melanie lives in Austin", kind="state"
        ),
        ExtractedFact(
            entity="Melanie", attribute="city", value="Melanie lived in Boston", kind="state"
        ),
    ]
    eng = _engine()
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(facts))
    await eng.start()
    try:
        await _write_session(eng)
        stats = await eng.sleep()
        assert stats["mine_facts"]["list_cards"]["cards"] == 0
        assert _live_cards(await _semantic(eng)) == []
    finally:
        await eng.stop()


async def test_option_off_writes_no_card(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(list_cards=False)
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(_activities()))
    await eng.start()
    try:
        await _write_session(eng)
        stats = await eng.sleep()
        assert "list_cards" not in stats["mine_facts"]
        assert _live_cards(await _semantic(eng)) == []
    finally:
        await eng.stop()


async def test_multiview_persons_and_topic_group_the_card(monkeypatch: pytest.MonkeyPatch) -> None:
    """#28 + #30: a shared event lands on both people's cards, grouped by topic."""
    facts = [
        ExtractedFact(
            entity="Melanie",
            attribute="event",
            value=value,
            date=day,
            persons=["Melanie", "Caroline"] if name == "camping" else ["Melanie"],
            topic="Activities",
        )
        for name, value, day in ACTIVITIES[:3]
    ] + [
        ExtractedFact(
            entity="Caroline",
            attribute="event",
            value="Caroline went hiking",
            date="2023-06-20",
            persons=["Caroline"],
            topic="activities",
        )
    ]
    eng = _engine(mine_multiview=True)
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(facts))
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        cards = {c.entity: c for c in _live_cards(await _semantic(eng))}
        assert set(cards) == {"Melanie", "Caroline"}
        assert cards["Melanie"].content.startswith("Melanie — activities: ")
        assert cards["Melanie"].content.count("(2023-") == 3
        assert "camping" in cards["Caroline"].content and "hiking" in cards["Caroline"].content
        assert "topic:activities" in cards["Melanie"].tags
        assert "person:melanie" in cards["Melanie"].tags
    finally:
        await eng.stop()


# -- the cards header shows the list card whole ----------------------------------------


async def test_cards_header_renders_the_list_card(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _engine(read={"cards": "header", "cards_budget_share": 0.5})
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(_activities()))
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        out = await eng.read(
            "What activities does Melanie do?", namespace="a", mode="retrieve", budget_tokens=800
        )
        [header] = [r for r in out.context.records if constants.CARDS_TAG in r.tags]
        line = next(x for x in header.content.splitlines() if x.startswith("Melanie — "))
        assert "(2023-05)" in line and "(2023-09)" in line and "[said" not in line
    finally:
        await eng.stop()


async def test_cards_header_skips_a_list_card_over_its_share(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _engine(read={"cards": "header", "cards_budget_share": 0.05})
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(_activities()))
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        out = await eng.read(
            "What activities does Melanie do?", namespace="a", mode="retrieve", budget_tokens=800
        )
        for header in (r for r in out.context.records if constants.CARDS_TAG in r.tags):
            assert "Melanie — " not in header.content
            assert len(header.content) // 4 <= 40
    finally:
        await eng.stop()


# -- the LLM labeller only for unclassed facts ----------------------------------------


class _Extract:
    """A stub ``extract`` provider: the miner reply, and the class labeller."""

    def __init__(self) -> None:
        self.class_calls = 0
        self.mine_calls = 0

    @property
    def provider_id(self) -> str:
        return "stub:extract"

    async def chat(self, messages: list[dict[str, str]], **options: Any) -> str:
        if "You group facts" in messages[0]["content"]:
            self.class_calls += 1
            return (
                "classes:\n  - {index: 1, label: books read}\n  - {index: 2, label: Books Read}\n"
            )
        self.mine_calls += 1
        return (
            "facts:\n"
            "  - {entity: Melanie, attribute: event, value: Melanie read Matilda,"
            " kind: event}\n"
            "  - {entity: Melanie, attribute: event, value: Melanie read Dune,"
            " kind: event}\n"
            "  - {entity: Melanie, attribute: hobby, value: Melanie paints, kind: event}\n"
            "  - {entity: Melanie, attribute: hobby, value: Melanie does pottery, kind: event}\n"
        )


def _llm_engine(monkeypatch: pytest.MonkeyPatch, stub: _Extract) -> Engine:
    stubs: dict[str, LLMService] = {"extract": stub}

    async def router(self: Engine, config: MemspineConfig) -> LLMRouter:
        return LLMRouter(stubs)

    monkeypatch.setattr(Engine, "_build_llm_router", router)
    return _engine()


async def test_labeller_is_called_once_per_person_for_generic_facts_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _Extract()
    eng = _llm_engine(monkeypatch, stub)
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        assert stub.class_calls == 1  # the two "event" facts, one batch; hobbies are deterministic
        cards = {c.content.split(":", 1)[0]: c for c in _live_cards(await _semantic(eng))}
        assert set(cards) == {"Melanie — books read", "Melanie — hobby"}
        await eng.sleep()
        assert stub.class_calls == 1  # cached as a list_classes marker, never asked again
    finally:
        await eng.stop()


# -- pure helpers ---------------------------------------------------------------------


def _fact(content: str, entity: str, tags: list[str], when: datetime = T0) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=content,
        entity=entity,
        valid_from=when,
        tags=["atomic_fact", "kind:event", *tags],
        source=SourceInfo(role="assistant", channel="mining"),
    )


def test_class_and_person_resolution() -> None:
    plain = _fact("Melanie hobby: Melanie paints", "Melanie", [])
    assert fact_class(plain) == "hobby" and fact_persons(plain) == ["melanie"]
    generic = _fact("Melanie event: Melanie read a book", "Melanie", [])
    assert fact_class(generic) is None
    tagged = _fact("Melanie event: x", "Melanie", ["topic:books read", "person:caroline"])
    assert fact_class(tagged) == "books read" and fact_persons(tagged) == ["caroline"]
    assert class_key("Activities") == class_key("activity") == "activity"
    assert class_key("books read") == "books read" and class_key("glass") == "glass"


def test_render_dedupes_and_caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(constants, "LIST_CARD_MAX_ITEMS", 2)
    members = [
        _fact("Melanie hobby: a", "Melanie", [], datetime(2023, 1, 5, tzinfo=UTC)),
        _fact("Melanie hobby: a", "Melanie", [], datetime(2023, 1, 9, tzinfo=UTC)),
        _fact("Melanie hobby: b", "Melanie", ["happened:2023-03-02"]),
        _fact("Melanie hobby: c", "Melanie", [], datetime(2023, 4, 1, tzinfo=UTC)),
    ]
    text = render_list_card("Melanie", "hobbies", members)
    assert text == "Melanie — hobbies: b (2023-03), c (2023-04) (+1 earlier)"


def test_list_card_tag_is_reserved() -> None:
    assert constants.LIST_CARD_TAG in constants.RESERVED_TAGS


async def test_every_rederivation_stays_live_not_quarantined(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A re-derived card repeats its predecessors' prefix by design: the MINJA
    bridge check must not hold the second, third, ... version."""
    eng = _engine()
    monkeypatch.setattr(eng, "_build_fact_miner", _mine(_activities()[:2]))
    await eng.start()
    try:
        await _write_session(eng)
        await eng.sleep()
        parents = [r.record_id for r in await eng.retrieve(namespace="a", memory_type="episodic")]
        for n, (_, value, day) in enumerate(ACTIVITIES[2:], 3):
            await eng._deposit_mined_fact(
                "a",
                f"Melanie activity: {value}",
                "Melanie",
                "activity",
                parents,
                datetime.fromisoformat(day).replace(tzinfo=UTC),
                f"s{n}",
                kind="event",
            )
            await eng.sleep()
            [card] = _live_cards(await _semantic(eng))
            assert not card.quarantined and card.content.count("(2023-") == n
    finally:
        await eng.stop()
