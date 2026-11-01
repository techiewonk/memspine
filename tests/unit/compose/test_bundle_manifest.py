"""ADR-092 / DS-13: the ``read.bundle_turns`` key and each packed hit's dependency manifest."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.compose.pack import estimate_tokens
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.store.records import SourceInfo


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
        memories={"semantic": {"enabled": True}, "episodic": {"enabled": True}},
        read={"record_access": False, "evidence_bundles": True, **read},
    )


_TURNS = (
    "I spent the weekend in Lisbon, the trip to Lisbon was lovely",
    "We walked around Lisbon and ate pastries in Belem",
    "The Lisbon trip ended with a late train home",
)


async def _seed(eng: Engine) -> tuple[list[str], str]:
    turns = [
        await eng.write(
            text,
            namespace="a",
            memory_type="episodic",
            source=SourceInfo(role="user", channel="chat"),
        )
        for text in _TURNS
    ]
    fact = await eng.write(
        "The user visited Lisbon",
        namespace="a",
        entity="user",
        attribute="visited",
        tags=["atomic_fact"],
        derived_from=[t.record_id for t in turns],
        source=SourceInfo(role="assistant", channel="mining"),
    )
    return [t.record_id for t in turns], fact.record_id


def test_bundle_turns_key_defaults_to_the_constant() -> None:
    assert ReadConfig().bundle_turns == constants.BUNDLE_TURNS == 2
    assert ReadConfig(bundle_turns=5).bundle_turns == 5
    with pytest.raises(ValueError):
        ReadConfig(bundle_turns=0)


async def test_cut_needed_turn_marks_the_fact_partial() -> None:
    eng = _engine()  # bundle_turns: 2 of the 3 source turns
    await eng.start()
    try:
        turn_ids, fact_id = await _seed(eng)
        ctx = await eng.assemble("Where did the user travel Lisbon", namespace="a")
        hit = ctx.hits[fact_id]
        assert set(hit.needed_turns) == set(turn_ids)
        assert len(hit.included_turns) == 2 and len(hit.dropped_turns) == 1
        assert set(hit.included_turns) | set(hit.dropped_turns) == set(turn_ids)
        assert hit.status == "partial" and fact_id in ctx.partial_hits
        shown = {r.record_id for r in ctx.records}
        assert set(hit.included_turns) <= shown
    finally:
        await eng.stop()


async def test_all_needed_turns_shown_is_complete() -> None:
    eng = _engine(bundle_turns=3)
    await eng.start()
    try:
        turn_ids, fact_id = await _seed(eng)
        ctx = await eng.assemble(
            "Where did the user travel Lisbon", namespace="a", budget_tokens=4096
        )
        hit = ctx.hits[fact_id]
        assert set(hit.included_turns) == set(turn_ids) and hit.dropped_turns == ()
        assert hit.status == "complete" and fact_id not in ctx.partial_hits
    finally:
        await eng.stop()


async def test_bundle_turns_one_shows_one_turn() -> None:
    eng = _engine(bundle_turns=1)
    await eng.start()
    try:
        _, fact_id = await _seed(eng)
        ctx = await eng.assemble("Where did the user travel Lisbon", namespace="a")
        hit = ctx.hits[fact_id]
        assert len(hit.included_turns) == 1 and len(hit.dropped_turns) == 2
    finally:
        await eng.stop()


async def test_engine_budget_sweep_never_exceeded_with_bundles() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _seed(eng)
        await eng.write(
            " ".join(f"Lisbon note {i} is long." for i in range(120)),
            namespace="a",
            memory_type="episodic",
        )
        for budget in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512):
            ctx = await eng.assemble("Lisbon trip", namespace="a", budget_tokens=budget)
            spent = sum(estimate_tokens(r.content) for r in ctx.records)
            if ctx.budget_status == "within":
                assert ctx.tokens_used <= budget, (budget, ctx.tokens_used)
                assert spent <= budget + len(ctx.records), (budget, spent)
            for rid in ctx.hits:
                assert rid in {r.record_id for r in ctx.records}
    finally:
        await eng.stop()
