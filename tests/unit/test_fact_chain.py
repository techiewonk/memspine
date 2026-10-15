"""E01 write-time projection arm (``memories.semantic.policies.fact_projection``): assertion
validation and the projection stage. Fake LLMs only. The read-time chain join was retired
(G-22)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from memspine import Engine
from memspine.core.fact_chain import (
    fact_projection_on,
    norm_entity,
    slot_name,
    validate_assertions,
)
from memspine.exceptions import ConfigError
from memspine.prompts.models import AssertionOut

# -- pure parts -----------------------------------------------------------------------------


def test_normalisation_is_exact_not_fuzzy() -> None:
    assert norm_entity("The Sven's") == norm_entity("sven") == "sven"
    assert norm_entity("he") == "" and norm_entity("") == ""
    assert slot_name("home_country") == slot_name("his home country") == "home country"
    assert slot_name("Sven's home country") == "home country"
    assert norm_entity("Sweden") != norm_entity("Swedish")


def test_spans_must_be_in_their_line() -> None:
    lines = ["Sven: my home country is Sweden", "Anna: nothing here"]
    good = AssertionOut(
        subject="Sven",
        relation="home_country",
        object="Sweden",
        line=1,
        span="my home country is Sweden",
    )
    paraphrase = AssertionOut(
        subject="Sven",
        relation="home_country",
        object="Sweden",
        line=2,
        span="Sven comes from Sweden",
    )
    wrong_line = AssertionOut(
        subject="Sven", relation="home_country", object="Sweden", line=3, span="my home country"
    )
    no_span = AssertionOut(subject="Sven", relation="home_country", object="Sweden", line=1)
    got = validate_assertions([good, good, paraphrase, wrong_line, no_span], lines)
    assert [(a.subject, a.line) for a in got] == [("Sven", 1)]  # duplicates dropped too


@pytest.mark.parametrize(
    ("value", "want"),
    [("off", False), (False, False), (None, False), ("on", True), (True, True), ("ON", True)],
)
def test_fact_projection_switch(value: Any, want: bool) -> None:
    assert fact_projection_on({"fact_projection": value}) is want
    assert fact_projection_on({}) is False
    with pytest.raises(ValueError):
        fact_projection_on({"fact_projection": "maybe"})


# -- the write-time arm -----------------------------------------------------------------------


SECRET = "Caroline: my passport number is X4471920"


def _projecting_engine(value: Any = "on") -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={
            # sessions come from the consolidate stage, which needs an active trigger
            "episodic": {"enabled": True, "policies": {"consolidation": {"mine_facts": True}}},
            "semantic": {"enabled": True, "policies": {"fact_projection": value}},
        },
    )


async def _session(eng: Engine, lines: list[str], ns: str = "a", sid: str = "s1") -> list[str]:
    t0 = datetime(2023, 5, 8, 13, 0, tzinfo=UTC)
    msgs = [
        {"role": "user", "content": c, "timestamp": (t0 + timedelta(minutes=i)).isoformat()}
        for i, c in enumerate(lines)
    ]
    await eng.write_messages(msgs, namespace=ns, session_id=sid, group_id=sid)
    turns = await eng.retrieve(namespace=ns, memory_type="episodic")
    by_text = {t.content: t.record_id for t in turns}
    return [by_text[next(k for k in by_text if line.split(": ", 1)[1] in k)] for line in lines]


def _projector(*items: AssertionOut) -> Any:
    async def project(transcript: str) -> list[AssertionOut]:
        return list(items)

    return project


def _passport(
    line: int = 1, span: str = "my passport number is X4471920", mode: str = "asserted"
) -> AssertionOut:
    return AssertionOut(
        subject="Caroline",
        relation="passport_number",
        object="X4471920",
        line=line,
        span=span,
        modality=mode,
    )


async def _projected(eng: Engine, ns: str = "a") -> list[Any]:
    return [
        r
        for r in await eng._require_started().list_records(ns, "semantic")
        if "fact_projection" in r.tags and r.status.value == "activated"
    ]


async def test_projection_is_off_by_default_and_adds_no_stage() -> None:
    eng = _projecting_engine("off")
    await eng.start()
    try:
        await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        assert "project_facts" not in await eng.sleep()
    finally:
        await eng.stop()


async def test_projection_writes_sourced_atomic_facts_once(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(_passport()))
    await eng.start()
    try:
        [turn_id, *_] = await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        stats = await eng.sleep()
        assert stats["project_facts"]["projected"] == 1 and stats["project_facts"]["errors"] == []
        [fact] = await _projected(eng)
        assert fact.source.parents == [turn_id]  # exactly the turn that holds the span
        assert "atomic_fact" in fact.tags and fact.namespace == "a"
        assert fact.content.startswith("Caroline passport_number: X4471920")
        again = await eng.sleep()
        assert again["project_facts"]["projected"] == 0 and len(await _projected(eng)) == 1
    finally:
        await eng.stop()


async def test_projection_drops_unsourced_and_non_asserted(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    items = (_passport(span="Caroline lost her passport"), _passport(mode="negated"))
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(*items))
    await eng.start()
    try:
        await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        stats = await eng.sleep()
        assert stats["project_facts"]["projected"] == 0 and await _projected(eng) == []
    finally:
        await eng.stop()


async def test_forgetting_the_parent_removes_the_projected_fact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    eng = _projecting_engine()
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(_passport()))
    await eng.start()
    try:
        [turn_id, *_] = await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        await eng.sleep()
        assert len(await _projected(eng)) == 1
        await eng.forget(turn_id, namespace="a", hard=True)
        assert await _projected(eng) == []
        assert (await eng.verify_forget(turn_id, namespace="a"))["clean"] is True
        assert not any(
            "X4471920" in r.content for r in await eng._require_started().list_records("a")
        )
        assert (await eng.sleep())["project_facts"]["projected"] == 0  # never re-derived
    finally:
        await eng.stop()


async def test_a_forget_during_projection_leaves_no_fact(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    entered, release = asyncio.Event(), asyncio.Event()

    async def blocking(transcript: str) -> list[AssertionOut]:
        entered.set()
        await release.wait()
        return [_passport()]

    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: blocking)
    await eng.start()
    try:
        [turn_id, *_] = await _session(eng, [SECRET, "Melanie: noted", "Caroline: thanks"])
        sleeper = asyncio.create_task(eng.sleep())
        await asyncio.wait_for(entered.wait(), 10)
        await eng.forget(turn_id, namespace="a", hard=True)  # I72: the parent goes meanwhile
        release.set()
        stats = await sleeper
        assert stats["project_facts"]["errors"] == [] and await _projected(eng) == []
        assert (await eng.verify_forget(turn_id, namespace="a"))["clean"] is True
    finally:
        await eng.stop()


async def test_projected_facts_never_cross_namespaces(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = _projecting_engine()
    monkeypatch.setattr(eng, "_build_assertion_projector", lambda: _projector(_passport()))
    await eng.start()
    try:
        [a_turn, *_] = await _session(
            eng, [SECRET, "Melanie: noted", "Caroline: thanks"], ns="a", sid="s1"
        )
        [b_turn, *_] = await _session(
            eng, [SECRET, "Melanie: noted", "Caroline: thanks"], ns="b", sid="s2"
        )
        await eng.sleep()
        [fa], [fb] = await _projected(eng, "a"), await _projected(eng, "b")
        assert fa.source.parents == [a_turn] and fb.source.parents == [b_turn]
        await eng.forget(a_turn, namespace="a", hard=True)
        assert await _projected(eng, "a") == [] and len(await _projected(eng, "b")) == 1
    finally:
        await eng.stop()


async def test_an_unknown_projection_value_is_a_config_error() -> None:
    eng = _projecting_engine("maybe")
    with pytest.raises(ConfigError):
        await eng.start()
    await eng.stop()
