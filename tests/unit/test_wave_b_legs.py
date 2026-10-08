"""Wave B (gaps plan 2026-10-08): N59 entity leg, N63 statement probe, N62 weights by
question shape, N33 short-question BM25 weight, N40 speaker probe. All off by default."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.query_shape import question_shape, statement_form
from memspine.core.records import MemoryRecord
from memspine.core.temporal_query import entity_leg, named_terms

T0 = datetime(2023, 5, 1, 9, 0, tzinfo=UTC)


def test_keys_are_off_by_default() -> None:
    read = ReadConfig()
    assert read.leg_weights_by_shape == {}
    assert read.short_query_lexical_weight is None
    assert (read.entity_leg, read.statement_probe, read.speaker_probe) == (False, False, False)


@pytest.mark.parametrize(
    ("question", "statement"),
    [
        ("When did Ana go camping?", "Ana go camping"),
        ("What is Ana's favourite book?", "Ana's favourite book is"),
        ("Did Ben join a club?", "Ben join a club"),
        ("Where does Ben live?", "Ben live"),
        ("Tell me about camping", None),
        ("How many times did Ana paint?", None),
    ],
)
def test_statement_form(question: str, statement: str | None) -> None:
    assert statement_form(question) == statement


@pytest.mark.parametrize(
    ("question", "shape"),
    [
        ("When did Ana go camping?", "temporal"),
        ("How many books has Ben read?", "count"),
        ("What is Ana's favourite book?", "plain"),
    ],
)
def test_question_shape(question: str, shape: str) -> None:
    assert question_shape(question) == shape


def test_named_terms_skip_question_words_months_and_speakers() -> None:
    q = "When did Caroline go to the LGBTQ support group in May 2023?"
    assert named_terms(q, exclude=["caroline"]) == ["lgbtq", "2023"]


def _rec(content: str, minutes: int = 0) -> MemoryRecord:
    return MemoryRecord(
        namespace="a",
        memory_type="episodic",
        content=content,
        valid_from=T0 + timedelta(minutes=minutes),
    )


def test_entity_leg_ranks_by_names_matched() -> None:
    both = _rec("Ana: Paris in 2022 was lovely", 1)
    one = _rec("Ana: I want to see Paris", 2)
    none = _rec("Ana: I like tea", 3)
    hits = entity_leg("Did Ana visit Paris in 2022?", [none, one, both], 5, exclude=["ana"])
    assert [h.record_id for h in hits] == [both.record_id, one.record_id]


def test_entity_leg_is_empty_when_only_speakers_are_named() -> None:
    assert entity_leg("What does Ana like?", [_rec("Ana: tea")], 5, exclude=["ana"]) == []


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def _seeded(**read: Any) -> Engine:
    eng = _engine(**read)
    await eng.start()
    for i, text in enumerate(
        ["Ana: we flew to Paris for the weekend", "Ben: I painted the lake", "Ana: tea time"]
    ):
        await eng.write(text, namespace="a", memory_type="episodic", valid_from=T0 + timedelta(i))
    return eng


async def test_engine_entity_leg_is_named_and_finds_the_place() -> None:
    eng = await _seeded(entity_leg=True)
    try:
        legs = await eng._metadata_legs("a", "When did Ana go to Paris?", 5)
    finally:
        await eng.stop()
    entity = [leg for leg in legs if getattr(leg, "name", None) == "entity"]
    assert len(entity) == 1 and len(entity[0]) == 1


async def test_engine_speaker_probe_adds_a_leg_only_when_a_speaker_is_named() -> None:
    eng = await _seeded(speaker_probe=True)
    try:
        named = await eng._metadata_legs("a", "What did Ben paint?", 5)
        unnamed = await eng._metadata_legs("a", "What was painted?", 5)
    finally:
        await eng.stop()
    assert len(named) == 1 and unnamed == []


async def test_leg_weights_by_shape_and_short_question() -> None:
    eng = _engine(
        leg_weights={"vector": 1.0},
        leg_weights_by_shape={"temporal": {"temporal": 3.0}},
        short_query_lexical_weight=2.0,
    )
    await eng.start()
    try:
        temporal = eng._leg_weights_for(
            "When did Ana and her two kids go camping at the lake in the hills?"
        )
        short = eng._leg_weights_for("Ana's book?")
    finally:
        await eng.stop()
    assert temporal == {"vector": 1.0, "temporal": 3.0}
    assert short["lexical"] == 2.0


async def test_statement_probe_joins_the_search(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _seeded(statement_probe=True)
    seen: list[Any] = []
    real = eng._search

    async def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("probes"))
        return await real(*args, **kwargs)

    monkeypatch.setattr(eng, "_search", spy)
    try:
        await eng.assemble("When did Ana go to Paris?", namespace="a", budget_tokens=200)
    finally:
        await eng.stop()
    assert any(p and "Ana go to Paris" in p for p in seen)
