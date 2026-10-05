"""#61: the query encoder port (ADR-050): ``none`` is byte-identical, ``cues`` matches
a query to stored anticipatory cues without a model."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.core.records import MemoryRecord, SourceInfo
from memspine.services.query_encoder import (
    CueQueryEncoder,
    NoopQueryEncoder,
    QueryEncoder,
)

FILLER = [
    "Bob repaired the garden fence on Sunday",
    "Carol is reading a novel about sailing",
    "Dan bought a new bicycle for commuting",
    "Erin baked bread with rye flour",
    "Frank watched the football final",
    "Gina planted tomatoes and basil",
]
TARGET = "Alice mentioned she recently developed a severe nut allergy"
CUE = "what snacks should we buy for Alice's birthday party"
QUERY = "snacks to buy for Alice's birthday party"


def _engine(encoder: str | None, **read: Any) -> Engine:
    read_cfg: dict[str, Any] = {"hybrid": False, **read}
    if encoder is not None:
        read_cfg["query_encoder"] = encoder
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read=read_cfg,
    )


async def _populate(eng: Engine, cue_source: SourceInfo | None = None) -> str:
    for text in FILLER:
        await eng.write(text, namespace="a")
    target = await eng.write(TARGET, namespace="a")
    await eng.add_cues(target.record_id, [CUE], namespace="a", source=cue_source)
    return target.record_id


async def _ranked(eng: Engine, query: str, top_k: int = 3) -> list[tuple[str, float]]:
    # Rounded: recency moves with wall-clock write time between two engines.
    return [(r.content, round(score, 4)) for r, score in await eng.search(query, "a", top_k=top_k)]


def test_ports_satisfy_the_protocol() -> None:
    async def load(_ns: str) -> list[MemoryRecord]:
        return []

    assert isinstance(NoopQueryEncoder(), QueryEncoder)
    assert isinstance(CueQueryEncoder(load), QueryEncoder)


async def test_none_reads_are_byte_identical_to_the_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def never(*_args: Any) -> list[object]:
        raise AssertionError("query encoder consulted with read.query_encoder=none")

    results = []
    for encoder in (None, "none"):
        eng = _engine(encoder)
        monkeypatch.setattr(eng, "_encoder_legs", never)  # the off path never reaches it
        await eng.start()
        try:
            await _populate(eng)
            ctx = await eng.assemble(QUERY, namespace="a", budget_tokens=512)
            results.append((await _ranked(eng, QUERY, top_k=5), [r.content for r in ctx.records]))
        finally:
            await eng.stop()
    assert results[0] == results[1]


async def test_cues_encoder_retrieves_the_cued_record() -> None:
    eng = _engine("cues")
    await eng.start()
    try:
        target_id = await _populate(eng)
        encoded = await eng._encoder().encode("a", QUERY)
        assert [m.target_id for m in encoded.matches] == [target_id]
        assert encoded.expansions == (CUE,)
        hits = await eng.search(QUERY, "a", top_k=1)
        assert hits[0][0].record_id == target_id  # the cue resolves to its turn
    finally:
        await eng.stop()


async def test_without_the_encoder_the_cued_record_is_not_reached() -> None:
    eng = _engine("none")
    await eng.start()
    try:
        target_id = await _populate(eng)
        hits = await eng.search(QUERY, "a", top_k=1)
        assert hits[0][0].record_id != target_id  # the words alone do not reach it
    finally:
        await eng.stop()


async def test_low_trust_cues_are_ignored() -> None:
    eng = _engine("cues", cue_min_trust=0.99)
    await eng.start()
    try:
        target_id = await _populate(eng)
        hits = await eng.search(QUERY, "a", top_k=1)
        assert hits[0][0].record_id != target_id
    finally:
        await eng.stop()


async def test_index_reloads_from_storage_and_drops_forgotten_targets(tmp_path: Any) -> None:
    path = str(tmp_path / "m.db")
    eng = Engine(
        template="core",
        dotenv_path=None,
        storage={"path": path},
        embedding={"provider": "hash"},
        read={"hybrid": False, "query_encoder": "cues"},
    )
    await eng.start()
    try:
        target_id = await _populate(eng)
    finally:
        await eng.stop()
    await eng.start()  # a fresh index, loaded from the stored cues
    try:
        hits = await eng.search(QUERY, "a", top_k=1)
        assert hits[0][0].record_id == target_id
        await eng.forget(target_id, namespace="a", hard=True)
        hits = await eng.search(QUERY, "a", top_k=3)
        assert target_id not in [r.record_id for r, _ in hits]
    finally:
        await eng.stop()


async def test_cue_encoder_unit_overlap_threshold() -> None:
    cue = MemoryRecord(
        namespace="a",
        memory_type="semantic",
        content=CUE,
        tags=["anticipatory_cue"],
        source=SourceInfo(parents=["t1"]),
    )

    async def load(_ns: str) -> list[MemoryRecord]:
        return [cue]

    encoder = CueQueryEncoder(load)
    assert (await encoder.encode("a", "Alice birthday party snacks")).matches
    assert not (await encoder.encode("a", "Alice's favourite colour")).matches
    assert not (await encoder.encode("a", "what is it")).matches  # no content words
