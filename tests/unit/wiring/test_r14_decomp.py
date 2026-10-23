"""R14-M-12 / OBS-10: ``read.decompose_v2`` (map-reduce read for multi-entity questions).

Model-free: hash embedder, no reranker, invented names.
"""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.read import decompose as rules

KNOWN = {"bram", "ilse", "noor", "tomasz"}


def test_default_off() -> None:
    assert ReadConfig().decompose_v2 is False
    assert ReadConfig().decompose_v2_max_entities == 3


@pytest.mark.parametrize(
    ("query", "combine", "count"),
    [
        ("Which city have both Bram and Ilse visited?", "intersection", False),
        ("How many cities have both Bram and Ilse visited?", "intersection", True),
        ("Who visited more cities, Bram or Ilse?", "compare", False),
        ("Compare what Bram and Ilse cook", "compare", False),
        ("What did either Bram or Ilse plant?", "union", False),
        ("Did Bram move before Ilse?", "before-after", False),
    ],
)
def test_plan_operator(query: str, combine: str, count: bool) -> None:
    p = rules.plan(query, KNOWN)
    assert p is not None
    assert p.entities == ("Bram", "Ilse")
    assert (p.combine, p.count) == (combine, count)


def test_sub_queries_drop_the_other_entity_and_keep_the_predicate() -> None:
    p = rules.plan("Which city have both Bram and Ilse visited?", KNOWN)
    assert p is not None
    assert p.sub_queries == ("Which city have Bram visited?", "Which city have Ilse visited?")
    q = rules.plan("Who visited more cities, Bram or Ilse's friend Noor?", KNOWN)
    assert q is not None and q.entities == ("Bram", "Ilse", "Noor")
    assert all(n not in q.sub_queries[0] for n in ("Ilse", "Noor"))
    assert "visited more cities" in q.sub_queries[0]


@pytest.mark.parametrize(
    "query",
    [
        "Which city did Bram visit?",  # one entity
        "What did Bram tell Ilse?",  # two entities, no join word
        "Which city have both of them visited?",  # none named
        "What does the user and the assistant discuss?",  # roles are not entities
    ],
)
def test_plan_rejects(query: str) -> None:
    assert rules.plan(query, KNOWN | {"user", "assistant"}) is None


def test_max_entities_cuts_the_list() -> None:
    p = rules.plan("Did Bram, Ilse, Noor and Tomasz all go there together?", KNOWN, 2)
    assert p is not None and p.entities == ("Bram", "Ilse")


def test_hint_text() -> None:
    p = rules.plan("How many cities have both Bram and Ilse visited?", KNOWN)
    assert p is not None
    hint = rules.combine_hint(p)
    assert "Bram, Ilse" in hint and "EVERY entity" in hint and "count" in hint


# --- engine ------------------------------------------------------------------------------

_TURNS = [
    ("Bram", "Bram visited Lisbon last spring and loved the trams"),
    ("Bram", "Bram visited Porto during the autumn trip"),
    ("Ilse", "Ilse visited Lisbon in the summer with her sister"),
    ("Ilse", "Ilse visited Gdansk for the amber market"),
]
_FILLER = "weather chatter about the evening and the bus timetable number {i}"


def _engine(**read: Any) -> Engine:
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
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def _fill(eng: Engine) -> None:
    for who, text in _TURNS:
        await eng.write(f"{who}: {text}", namespace="a", memory_type="episodic")
    for i in range(30):
        await eng.write(f"Noor: {_FILLER.format(i=i)}", namespace="a", memory_type="episodic")


_Q = "Which city have both Bram and Ilse visited?"


async def _read(eng: Engine, **kw: Any) -> Any:
    return await eng.read(_Q, namespace="a", mode="retrieve", budget_tokens=400, **kw)


async def test_flag_off_is_the_plain_read() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fill(eng)
        res = await _read(eng)
        assert not any("[entity:" in r.content for r in res.context.records)
        assert not any("[combine]" in r.content for r in res.context.records)
    finally:
        await eng.stop()


@pytest.mark.parametrize("parallel", [False, True])
async def test_flag_on_groups_evidence_per_entity(parallel: bool) -> None:
    eng = _engine(decompose_v2=True, parallel_legs=parallel)
    await eng.start()
    try:
        await _fill(eng)
        res = await _read(eng)
        texts = [r.content for r in res.context.records]
        hint = next(i for i, t in enumerate(texts) if t.startswith("[combine]"))
        bram = next(i for i, t in enumerate(texts) if t.startswith("[entity: Bram]"))
        ilse = next(i for i, t in enumerate(texts) if t.startswith("[entity: Ilse]"))
        assert hint < bram < ilse
        assert "EVERY entity" in texts[hint]
        # each entity's block carries its own turns (quota: neither crowds the other out)
        assert any("Lisbon" in t and "Bram" in t for t in texts[bram:ilse])
        assert any("Lisbon" in t and "Ilse" in t for t in texts[ilse:]), texts
        # a record is listed once
        ids = [r.record_id for r in res.context.records if "lead_section" not in r.tags]
        assert len(ids) == len(set(ids))
        assert res.context.tokens_used > 0
    finally:
        await eng.stop()


async def test_single_entity_question_is_untouched() -> None:
    eng = _engine(decompose_v2=True)
    await eng.start()
    try:
        await _fill(eng)
        res = await eng.read(
            "Which city did Bram visit?", namespace="a", mode="retrieve", budget_tokens=400
        )
        assert not any("[entity:" in r.content for r in res.context.records)
    finally:
        await eng.stop()


async def test_whole_store_fitting_reads_as_before() -> None:
    eng = _engine(decompose_v2=True)
    await eng.start()
    try:
        for who, text in _TURNS:
            await eng.write(f"{who}: {text}", namespace="a", memory_type="episodic")
        res = await eng.read(_Q, namespace="a", budget_tokens=4000)
        assert res.mode == "full"
        assert not any("[entity:" in r.content for r in res.context.records)
    finally:
        await eng.stop()
