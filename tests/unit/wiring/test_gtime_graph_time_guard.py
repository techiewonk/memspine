"""GTIME: ``read.graph_time_guard`` (invented text, stub engine; no model call)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from memspine.compose.pipeline.pack import _read_headers
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.models.encoders.query.shape import is_temporal, is_time_shaped
from memspine.read.pipeline.time_guard import blocks_guarded, guard_mode, legs_guarded
from memspine.store.records import MemoryRecord

WHEN = "When did Ilse visit the lighthouse?"
OTHER_TIME = "Which year did Bram move to Lisbon?"
EXPLICIT = "What did Noor paint in March 2022?"
PLAIN = "What does Tomasz grow on his balcony?"


def test_default_is_off_and_keys_validate() -> None:
    cfg = ReadConfig()
    assert cfg.graph_time_guard == "off"
    assert cfg.graph_time_guard_shape == "temporal"
    with pytest.raises(ValueError):
        ReadConfig(graph_time_guard="always")


def test_time_shaped_extends_temporal() -> None:
    assert is_temporal(WHEN) and is_time_shaped(WHEN)
    bees = "How long has Ilse kept bees?"
    assert is_time_shaped(bees) and not is_temporal(bees)
    assert is_time_shaped(EXPLICIT) and not is_temporal(EXPLICIT)
    assert is_time_shaped("Who visited Bram on 3 May 2023?")
    assert not is_time_shaped(PLAIN)
    assert not is_time_shaped("What did Noor say about the last book?")


def test_guard_mode_by_shape_and_mode() -> None:
    off = ReadConfig()
    assert guard_mode(off, WHEN) == "off" and not blocks_guarded(off, WHEN)
    blocks = ReadConfig(graph_time_guard="blocks")
    assert guard_mode(blocks, WHEN) == "blocks" and blocks_guarded(blocks, WHEN)
    assert not legs_guarded(blocks, WHEN)
    assert guard_mode(blocks, PLAIN) == "off" and not blocks_guarded(blocks, PLAIN)
    assert guard_mode(blocks, EXPLICIT) == "off"  # default shape = temporal only
    wide = ReadConfig(graph_time_guard="blocks+legs", graph_time_guard_shape="time_shaped")
    assert legs_guarded(wide, EXPLICIT) and legs_guarded(wide, OTHER_TIME)
    assert not legs_guarded(wide, PLAIN)


def _block(tag: str) -> MemoryRecord:
    return MemoryRecord(
        record_id=tag,
        namespace="a",
        content=f"{tag} block",
        memory_type="semantic",
        tags=[constants.LEAD_TAG, tag],
    )


class _Eng:
    def __init__(self, read: ReadConfig) -> None:
        self._read = read
        self.called: list[str] = []

    def _config(self) -> Any:
        return SimpleNamespace(read=self._read)

    async def _none(self, *a: Any, **k: Any) -> None:
        return None

    _cards_section = _profile_section = _novelty_section = _slots_section = _none
    _session_digest_section = _recent_section = _none

    async def _graph_facts_section(self, *a: Any, **k: Any) -> MemoryRecord:
        self.called.append("facts")
        return _block(constants.GRAPH_FACTS_TAG)

    async def _entity_summary_section(self, *a: Any, **k: Any) -> MemoryRecord:
        self.called.append("about")
        return _block(constants.ENTITY_SUMMARIES_TAG)

    def _headers_cost(self, headers: Any) -> int:
        return 0


async def _headers(read: ReadConfig, query: str) -> tuple[list[str], list[MemoryRecord]]:
    eng = _Eng(read)
    out = await _read_headers(eng, "a", query, 4000)
    return eng.called, out


@pytest.mark.asyncio
async def test_blocks_are_left_out_of_a_when_question_only() -> None:
    base = {"entity_summaries": True, "cards_include_edges": True}
    called, got = await _headers(ReadConfig(**base), WHEN)
    assert set(called) == {"facts", "about"} and len(got) == 2  # off: unchanged
    on = ReadConfig(graph_time_guard="blocks", **base)
    called, got = await _headers(on, WHEN)
    assert called == [] and got == []
    called, got = await _headers(on, PLAIN)
    assert set(called) == {"facts", "about"} and len(got) == 2  # not a when question
    called, got = await _headers(on, EXPLICIT)
    assert len(got) == 2  # default shape does not cover a named date
    wide = ReadConfig(graph_time_guard="blocks", graph_time_guard_shape="time_shaped", **base)
    called, got = await _headers(wide, EXPLICIT)
    assert got == []


def _engine(**read: Any) -> Any:
    from memspine import Engine

    return Engine(
        template="core",
        dotenv_path=None,
        storage={'provider': "sqlite", 'settings': {'path': ":memory:", 'busy_timeout_s': 5.0}},
        embedding={'provider': "hash", 'settings': {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


async def _seed(eng: Any) -> tuple[list[str], str]:
    ids = []
    for text in (
        "Ilse said she adopted a dog named Biscuit in March",
        "Bram talked about his holiday in Lyon",
        "Noor asked about the weather forecast",
    ):
        w = await eng.write(text, namespace="a", memory_type="episodic")
        ids.append(w.record_id)
    fact = await eng.write(
        "Ilse adopted a dog named Biscuit", namespace="a", memory_type="semantic", derived_from=ids
    )
    return ids, fact.record_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("guard", "query", "lifted"),
    [
        ("off", "When did Ilse adopt the dog Biscuit?", True),
        ("blocks", "When did Ilse adopt the dog Biscuit?", True),  # blocks leave the lift alone
        ("blocks+legs", "When did Ilse adopt the dog Biscuit?", False),  # the lift is skipped
        ("blocks+legs", "Which dog did Ilse adopt, Biscuit?", True),  # not a when question
    ],
)
async def test_legs_guard_skips_the_lift_on_a_when_question(
    guard: str, query: str, lifted: bool
) -> None:
    eng = _engine(derived_lift="evidence+match", derived_lift_max_parents=1, graph_time_guard=guard)
    await eng.start()
    try:
        ids, fact_id = await _seed(eng)
        got = [r.record_id for r, _ in await eng.search(query, namespace="a", top_k=5)]
        assert (fact_id not in got) is lifted
        if lifted:
            assert ids[0] in got
    finally:
        await eng.stop()
