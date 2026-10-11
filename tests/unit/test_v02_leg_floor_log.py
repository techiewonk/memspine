"""V02: per-leg floor cuts and query-contract fields in ``search_forensics``."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.core import trace_sink
from memspine.engine import search_forensics


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
    for text in ("garden travel plans in spring", "football on Sunday", "a quiet note about tea"):
        await eng.write(text, namespace="a", memory_type="episodic")
    return eng


async def test_floor_drops_are_logged_per_leg_with_score_and_rank() -> None:
    eng = await _seeded(hybrid=False, leg_min_scores={"vector": 2.0})  # above any cosine
    try:
        with search_forensics() as fx:
            out = await eng.search("garden travel", namespace="a", top_k=3)
        cuts = [c for c in fx.get("cuts", []) if c["reason"] == "leg_floor"]
        assert not out  # the floor removed every vector hit (existing behaviour)
        assert cuts and {c["leg"] for c in cuts} == {"vector"}
        assert all(c["floor"] == 2.0 and c["score"] < 2.0 for c in cuts)
        assert sorted(c["leg_rank"] for c in cuts) == list(range(1, len(cuts) + 1))
    finally:
        await eng.stop()


async def test_lexical_floor_is_a_separate_leg() -> None:
    eng = await _seeded(leg_min_scores={"lexical": 1e9})
    try:
        with search_forensics() as fx:
            await eng.search("garden travel", namespace="a", top_k=3)
        legs = {c["leg"] for c in fx.get("cuts", []) if c["reason"] == "leg_floor"}
        assert legs == {"lexical"}
    finally:
        await eng.stop()


async def test_no_floor_means_no_leg_floor_cut() -> None:
    eng = await _seeded()
    try:
        with search_forensics() as fx:
            await eng.search("garden travel", namespace="a", top_k=3)
        assert not [c for c in fx.get("cuts", []) if c["reason"] == "leg_floor"]
    finally:
        await eng.stop()


async def test_logging_is_free_when_no_sink_is_active(monkeypatch: pytest.MonkeyPatch) -> None:
    eng = await _seeded(hybrid=False, leg_min_scores={"vector": 2.0})
    calls: list[tuple[Any, ...]] = []
    monkeypatch.setattr(trace_sink, "cut", lambda *a, **k: calls.append(a))
    try:
        await eng.search("garden travel", namespace="a", top_k=3)  # no search_forensics()
        assert calls == []
    finally:
        await eng.stop()


async def test_query_contract_fields_present_and_marked_absent_when_off() -> None:
    eng = await _seeded()
    try:
        with search_forensics() as fx:
            await eng.search("when did Tim visit Rome", namespace="a", top_k=3)
        assert fx["query_contract"] == {"mode": "off", "status": "absent"}
    finally:
        await eng.stop()


async def test_query_contract_fields_observed_when_on() -> None:
    eng = await _seeded(query_contract="heuristic")
    try:
        with search_forensics() as fx:
            await eng.search("when did Tim visit Rome", namespace="a", top_k=3)
        qc = fx["query_contract"]
        assert qc["status"] == "observed" and qc["mode"] == "heuristic"
        assert "answer_type" in qc and "use" in qc and "rerank_hint" in qc
    finally:
        await eng.stop()
