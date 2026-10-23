"""GLIFT: ``read.derived_lift`` (invented text, hash embedder, stub store; no model call)."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from memspine import Engine
from memspine.config import constants
from memspine.config.schema import ReadConfig
from memspine.read.pipeline.derived import derived_lift_stage
from memspine.store.records import MemoryRecord, RecordFingerprint, SourceInfo

AT = datetime(2024, 3, 2, 10, 0, tzinfo=UTC)


def _turn(rid: str, text: str) -> MemoryRecord:
    return MemoryRecord(
        record_id=rid, namespace="a", content=text, valid_from=AT, memory_type="episodic"
    )


def _fact(
    rid: str,
    text: str,
    parents: list[str],
    cited: list[str] | None = None,
    tags: list[str] | None = None,
) -> MemoryRecord:
    env = None
    if cited is not None:
        env = {
            "evidence": [
                {"role": "support", "turn": {"source_id": c, "source_version": 1, "seg_index": 0}}
                for c in cited
            ]
            + [
                {
                    "role": "context",
                    "turn": {"source_id": parents[0], "source_version": 1, "seg_index": 0},
                }
            ]
        }
    return MemoryRecord(
        record_id=rid,
        namespace="a",
        content=text,
        valid_from=AT,
        memory_type="semantic",
        tags=tags or [],
        source=SourceInfo(role="assistant", channel="extract_graph", parents=parents),
        fingerprint=RecordFingerprint(envelope=env),
    )


class _Store:
    def __init__(self, *records: MemoryRecord) -> None:
        self._by_id = {r.record_id: r for r in records}

    async def get_record(self, rid: str) -> MemoryRecord | None:
        return self._by_id.get(rid)


class _Vec:
    def __init__(self, scores: dict[str, float]) -> None:
        self._scores = scores
        self.calls = 0

    async def scores_for(
        self, ns: str, vector: list[float], embedder_id: str, ids: list[str]
    ) -> dict[str, float]:
        self.calls += 1
        return {i: self._scores[i] for i in ids if i in self._scores}


def _eng(*records: MemoryRecord, vec: _Vec | None = None, hidden: set[str] | None = None) -> Any:
    store = _Store(*records)

    async def gate(ns: str, pairs: list[Any], *_a: Any) -> list[Any]:
        return [
            (store._by_id[rid], sc)
            for rid, sc in pairs
            if rid in store._by_id and rid not in (hidden or set())
        ]

    return SimpleNamespace(
        _require_started=lambda: store,
        _gate_hits=gate,
        _vector=vec,
        _embedder=SimpleNamespace(embedder_id="stub"),
    )


T = {
    i: _turn(f"t{i}", f"Ilse: note {i} about {w}")
    for i, w in enumerate(
        ["the shed", "the market", "bees by the river", "a trip to Lyon", "painting the fence"], 1
    )
}
ALL = list(T.values())
FILL = [(_turn(f"x{i}", f"Bram: filler {i}"), 0.01) for i in range(3)]  # widen the incoming pool


async def _lift(eng: Any, cands: list[Any], query: str = "bees", **kw: Any) -> list[Any]:
    args: dict[str, Any] = {
        "mode": "evidence+match",
        "max_parents": 3,
        "group_id": None,
        "tags": None,
        "memory_type": None,
        "hide": None,
    }
    args.update(kw)
    return await derived_lift_stage(eng, "a", query, [0.1], cands, {}, **args)


def test_flag_defaults_off() -> None:
    cfg = ReadConfig()
    assert cfg.derived_lift == "off"
    assert cfg.derived_lift_max_parents == 3


@pytest.mark.asyncio
async def test_evidence_first_beats_session_wide_parents() -> None:
    fact = _fact("f1", "Ilse keeps bees", [t.record_id for t in ALL], cited=["t3"])
    out = await _lift(_eng(*ALL, fact), [(fact, 0.8)])
    assert [r.record_id for r, _ in out] == ["t3"]  # cited line only; context line ignored
    assert out[0][1] == 0.8  # inherits the derived score


@pytest.mark.asyncio
async def test_session_wide_parents_pick_the_most_similar_by_stored_vector() -> None:
    fact = _fact("f1", "Ilse had a busy week", [t.record_id for t in ALL])
    vec = _Vec({"t1": 0.1, "t2": 0.2, "t3": 0.9, "t4": 0.3, "t5": 0.8})
    out = await _lift(_eng(*ALL, fact, vec=vec), [(fact, 0.5), *FILL], max_parents=2)
    assert [r.record_id for r, _ in out][:2] == ["t3", "t5"]
    assert vec.calls == 1


@pytest.mark.asyncio
async def test_lexical_fallback_without_vector_scores() -> None:
    fact = _fact("f1", "Ilse had a busy week", [t.record_id for t in ALL])
    out = await _lift(_eng(*ALL, fact), [(fact, 0.5)], query="bees by the river", max_parents=1)
    assert [r.record_id for r, _ in out] == ["t3"]


@pytest.mark.asyncio
async def test_evidence_mode_keeps_lineage_order_not_similarity() -> None:
    fact = _fact("f1", "Ilse had a busy week", [t.record_id for t in ALL])
    vec = _Vec({"t5": 0.99})
    cands = [(fact, 0.5), *FILL]
    out = await _lift(_eng(*ALL, fact, vec=vec), cands, mode="evidence", max_parents=2)
    assert [r.record_id for r, _ in out][:2] == ["t1", "t2"]
    assert vec.calls == 0


@pytest.mark.asyncio
async def test_derived_never_stays_and_scores_dedupe_to_the_max() -> None:
    f1 = _fact("f1", "fact one", ["t1", "t2"])
    f2 = _fact("f2", "fact two", ["t2"])
    cands = [(f1, 0.9), (T[2], 0.4), (f2, 0.6), (T[4], 0.3)]
    out = await _lift(_eng(*ALL, f1, f2), cands)
    ids = [r.record_id for r, _ in out]
    assert "f1" not in ids and "f2" not in ids
    assert ids == ["t1", "t2", "t4"]  # sorted by score, t2 deduplicated
    assert dict((r.record_id, s) for r, s in out)["t2"] == 0.9


@pytest.mark.asyncio
async def test_dropped_when_no_visible_source_turn_and_lead_block_untouched() -> None:
    orphan = _fact("f1", "Noor moved", ["gone"])
    hidden_src = _fact("f2", "Bram left", ["t1"])
    lead = _fact("lead", "ABOUT block", ["t1"], tags=[constants.LEAD_TAG])
    cands = [(orphan, 0.9), (hidden_src, 0.8), (lead, 0.7), (T[2], 0.1)]
    out = await _lift(_eng(*ALL, orphan, hidden_src, lead, hidden={"t1"}), cands)
    assert [r.record_id for r, _ in out] == ["lead", "t2"]


@pytest.mark.asyncio
async def test_pool_is_cut_back_to_its_incoming_size() -> None:
    fact = _fact("f1", "Ilse had a busy week", [t.record_id for t in ALL])
    out = await _lift(_eng(*ALL, fact), [(fact, 0.5)], mode="evidence", max_parents=3)
    assert len(out) == 1  # incoming pool of 1: cut back


@pytest.mark.asyncio
async def test_no_derived_hits_returns_the_same_list() -> None:
    cands = [(T[1], 0.5), (T[2], 0.4)]
    assert await _lift(_eng(*ALL), cands) is cands


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


async def _store_with_fact(eng: Engine) -> tuple[list[str], str]:
    ids = []
    for text in (
        "Ilse said she adopted a dog named Biscuit in March",
        "Bram talked about his holiday in Lyon",
        "Noor asked about the weather forecast",
    ):
        w = await eng.write(text, namespace="a", memory_type="episodic")
        ids.append(w.record_id if hasattr(w, "record_id") else str(w))
    fact = await eng.write(
        "Ilse adopted a dog named Biscuit", namespace="a", memory_type="semantic", derived_from=ids
    )
    return ids, fact.record_id if hasattr(fact, "record_id") else str(fact)


@pytest.mark.asyncio
async def test_engine_search_on_replaces_fact_by_its_best_turn() -> None:
    eng = _engine(derived_lift="evidence+match", derived_lift_max_parents=1)
    await eng.start()
    try:
        ids, fact_id = await _store_with_fact(eng)
        out = await eng.search("Ilse dog Biscuit", namespace="a", top_k=5)
        got = [r.record_id for r, _ in out]
        assert fact_id not in got
        assert ids[0] in got
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_engine_search_off_keeps_fact() -> None:
    eng = _engine()
    await eng.start()
    try:
        _, fact_id = await _store_with_fact(eng)
        out = await eng.search("Ilse dog Biscuit", namespace="a", top_k=5)
        assert fact_id in [r.record_id for r, _ in out]
    finally:
        await eng.stop()


@pytest.mark.asyncio
async def test_engine_read_on_never_puts_the_derived_record_in_context() -> None:
    eng = _engine(derived_lift="evidence+match")
    await eng.start()
    try:
        ids, fact_id = await _store_with_fact(eng)
        out = await eng.read("Ilse dog Biscuit", namespace="a", mode="replay")
        shown = [r.record_id for r in out.context.records]
        assert fact_id not in shown
        assert ids[0] in shown
    finally:
        await eng.stop()


async def test_lancedb_scores_for_matches_query_cosine(tmp_path: Any) -> None:
    from memspine.clients.lancedb import LanceDBClient, LanceDBSpec
    from memspine.services.embedding.hash import HashEmbedding
    from memspine.services.vector.lancedb_store import LanceDBVectorStore

    client = LanceDBClient(LanceDBSpec(tmp_path / "vec.lance"))
    await client.open()
    emb = HashEmbedding(dim=16)
    store = LanceDBVectorStore(client, emb)
    vecs = {"a": [1.0] + [0.0] * 15, "b": [0.0, 1.0] + [0.0] * 14, "c": [1.0, 1.0] + [0.0] * 14}
    for rid, v in vecs.items():
        await store.upsert(rid, "n", emb.embedder_id, v)
    got = await store.scores_for("n", vecs["a"], emb.embedder_id, ["b", "c", "zz"])
    assert set(got) == {"b", "c"}  # only the asked ids; an unknown id is absent
    assert abs(got["b"]) < 1e-6 and abs(got["c"] - 0.7071) < 1e-3
    assert await store.scores_for("n", vecs["a"], emb.embedder_id, []) == {}
