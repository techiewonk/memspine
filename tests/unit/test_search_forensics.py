"""``memspine.engine.search_forensics()``: opt-in stage capture for evals and debugging (PROC-2)."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.engine import search_forensics

TEXTS = [
    "Melanie went kayaking on the lake",
    "The bus was late again",
    "Pottery class today",
    "Kayaking gear is stored in the garage",
]


def _engine() -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": True, "record_access": False},
    )


async def _fill(eng: Engine) -> None:
    for text in TEXTS:
        await eng.write(text, namespace="a", memory_type="episodic")


def _ids(pairs: list[tuple[str, float]]) -> list[str]:
    return [rid for rid, _ in pairs]


async def test_records_every_stage_for_a_search() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fill(eng)
        with search_forensics() as stages:
            hits = await eng.search("kayaking", namespace="a", top_k=3)
        for stage in ("vector", "lexical", "fused", "pool", "final"):
            assert stage in stages, stage
            assert stages[stage], f"{stage} is empty"
            assert all(isinstance(rid, str) for rid, _ in stages[stage])
        assert "extra_legs" in stages
        assert "reranker" in stages
        # The final stage is exactly what the caller got back, in order.
        assert _ids(stages["final"]) == [str(r.record_id) for r, _ in hits]
        # Every returned record came out of the candidate pool.
        assert set(_ids(stages["final"])) <= set(_ids(stages["pool"]))
    finally:
        await eng.stop()


async def test_no_op_when_not_used() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fill(eng)
        plain = await eng.search("kayaking", namespace="a", top_k=3)
        with search_forensics() as stages:
            captured = await eng.search("kayaking", namespace="a", top_k=3)
        # Capturing changes nothing about the result.
        assert [str(r.record_id) for r, _ in plain] == [str(r.record_id) for r, _ in captured]
        # A search after the block leaves the closed sink untouched.
        snapshot = dict(stages)
        await eng.search("pottery", namespace="a", top_k=3)
        assert stages == snapshot
    finally:
        await eng.stop()


async def test_sinks_do_not_leak_between_blocks() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fill(eng)
        sinks: list[dict[str, Any]] = []
        for query in ("kayaking", "pottery"):
            with search_forensics() as stages:
                await eng.search(query, namespace="a", top_k=2)
            sinks.append(stages)
        assert sinks[0] is not sinks[1]
        assert _ids(sinks[0]["final"]) != _ids(sinks[1]["final"])
    finally:
        await eng.stop()
