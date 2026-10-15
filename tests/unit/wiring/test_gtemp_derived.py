"""GTEMP: ``read.derived_dates`` and ``read.derived_cap`` (invented text, no model)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.core.event_date import HAPPENED_PREFIX
from memspine.read.pipeline.derived import place_derived, redate_derived, source_turn
from memspine.store.records import MemoryRecord, SourceInfo

SESSION_CLOSE = datetime(2024, 3, 9, 20, 0, tzinfo=UTC)
SAID = datetime(2024, 3, 2, 10, 0, tzinfo=UTC)
OTHER = datetime(2024, 3, 9, 9, 0, tzinfo=UTC)


def _turn(rid: str, text: str, at: datetime) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid, namespace="a", content=text, valid_from=at, memory_type="episodic"
    )


def _fact(rid: str, text: str, parents: list[str], tags: list[str] | None = None) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid,
        namespace="a",
        content=text,
        valid_from=SESSION_CLOSE,
        memory_type="semantic",
        tags=tags or [],
        source=SourceInfo(role="assistant", channel="extract_graph", parents=parents),
    )


class _Store:
    def __init__(self, *records: MemoryRecord) -> None:
        self._by_id = {r.record_id: r for r in records}

    async def get_record(self, rid: str) -> MemoryRecord | None:
        return self._by_id.get(rid)


def _eng(*records: MemoryRecord) -> Any:
    store = _Store(*records)
    return SimpleNamespace(_require_started=lambda: store)


T1 = _turn("t1", "Ilse: I repainted the shed yesterday with Bram", SAID)
T2 = _turn("t2", "Bram: Nice, see you at the market", OTHER)
FACT = _fact("f1", "Ilse repainted the shed", ["t1", "t2"], [constants.UNDATED_TAG])


def test_flags_default_off() -> None:
    cfg = ReadConfig()
    assert cfg.derived_dates is False
    assert cfg.derived_cap == 0


@pytest.mark.asyncio
async def test_source_turn_is_best_overlap() -> None:
    got = await source_turn(_eng(T1, T2), "a", FACT)
    assert got is not None and got.record_id == "t1"


@pytest.mark.asyncio
async def test_redate_uses_source_turn_date_and_clears_undated_tag() -> None:
    query = "What colour is the shed?"
    [(out, score)] = await redate_derived(_eng(T1, T2), "a", query, [(FACT, 0.7)])
    assert out.valid_from == SAID
    assert constants.UNDATED_TAG not in out.tags
    assert score == 0.7
    assert FACT.valid_from == SESSION_CLOSE  # the stored record is untouched


@pytest.mark.asyncio
async def test_dateless_derived_dropped_only_on_temporal_question() -> None:
    orphan = _fact("f2", "Noor moved to Lyon", ["gone"], [constants.UNDATED_TAG])
    eng = _eng(T1)
    kept = await redate_derived(eng, "a", "Where does Noor live?", [(orphan, 0.5)])
    assert [r.record_id for r, _ in kept] == ["f2"]
    dropped = await redate_derived(eng, "a", "When did Noor move to Lyon?", [(orphan, 0.5)])
    assert dropped == []


@pytest.mark.asyncio
async def test_turns_and_happened_facts_pass_through_unchanged() -> None:
    happened = _fact("f3", "Tomasz ran a race", ["t1"], [f"{HAPPENED_PREFIX}2024-02-01"])
    query = "When did Tomasz run?"
    out = await redate_derived(_eng(T1), "a", query, [(T2, 0.9), (happened, 0.4)])
    assert [r for r, _ in out] == [T2, happened]


def test_place_derived_caps_and_orders_after_turns() -> None:
    lead = _fact("lead", "ABOUT block", ["t1"]).model_copy(update={"tags": [constants.LEAD_TAG]})
    f1, f2, f3 = (_fact(f"f{i}", f"fact {i}", ["t1"]) for i in (1, 2, 3))
    front, rest = place_derived([lead, f1, f2, f3], [T1, T2], cap=2)
    assert [r.record_id for r in front] == ["lead"]
    assert [r.record_id for r in rest] == ["t1", "t2", "f1", "f2"]


def test_place_derived_keeps_non_derived_stable_records_in_front() -> None:
    persona = MemoryRecord(
        record_id="p", namespace="a", content="persona", valid_from=SAID, memory_type="working"
    )
    front, rest = place_derived([persona, FACT], [T1], cap=1)
    assert [r.record_id for r in front] == ["p"]
    assert [r.record_id for r in rest] == ["t1", "f1"]


@pytest.mark.asyncio
async def test_replay_read_runs_with_both_flags_on() -> None:
    from memspine import Engine

    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "rerank": "off", "derived_dates": True, "derived_cap": 1},
    )
    async with eng:
        await eng.write("Ilse moved to Lyon last spring", namespace="a", memory_type="episodic")
        await eng.write("Bram keeps bees near the river", namespace="a", memory_type="episodic")
        out = await eng.read("When did Ilse move to Lyon?", namespace="a", mode="replay")
    assert out.context.records
