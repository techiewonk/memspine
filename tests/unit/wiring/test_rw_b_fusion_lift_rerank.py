"""RW-B: R3-06 family fusion, R3-08 lift before rerank, R3-07 rerank v2 (flag-off by default)."""

from __future__ import annotations

from typing import Any

from memspine import Engine
from memspine.config.schema import ReadConfig, RerankConfig
from memspine.observability.trace import search_forensics
from memspine.read.pipeline.fusion import fuse_families, schema_family
from memspine.read.pipeline.lift import lineage_from_records
from memspine.services.rerank.jina import JinaReranker

_GOLD = "zyzzyva"


#: The old ``read.rerank_v2: true`` default chain (ADR-082: one ``model.rerank.chain`` entry each).
_V2_CHAIN = [{"provider": p, "settings": {}} for p in ("jina", "qwen3", "onnx")]


def _engine(rerank: dict[str, Any] | None = None, **read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={"embedding": {"provider": "hash", "settings": {}}, "rerank": rerank or {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, **read},
    )


class _Hit:
    def __init__(self, rid: str, score: float = 1.0) -> None:
        self.record_id = rid
        self.score = score


def test_defaults_off() -> None:
    cfg = ReadConfig()
    assert (cfg.family_fusion, cfg.lift_before_rerank) == (False, False)
    rerank = RerankConfig()
    assert rerank.chain == []  # ADR-082: an empty chain is off
    assert rerank.pool == 64


def test_schema_family_reconciliation() -> None:
    assert schema_family("semantic") == "dense"
    assert schema_family("lexical") == "lexical"
    assert schema_family("temporal") == "structured"
    assert schema_family("other", derived=True) == "derived"


def test_fuse_families_protects_and_floors() -> None:
    vec = [_Hit(f"v{i}", 0.9 - i * 0.01) for i in range(10)]
    lex = [_Hit("gold", 0.05), *[_Hit(f"v{i}", 5.0) for i in range(3)]]
    ranked, protected, slots = fuse_families(
        vec,
        lex,
        True,
        [],
        pool_size=4,
        protect_n=1,
        rrf_k=60,
        floors={"lexical": 1.0},
    )
    ids = [r for r, _ in ranked]
    assert "gold" in ids  # below the lexical floor but protected (top of its family)
    assert protected["gold"] == "lexical"
    assert slots["lexical"] == ["gold"]
    assert all(0.0 <= s <= 1.0 for _, s in ranked)
    assert len(ids) <= 4


async def test_family_fusion_keeps_bm25_only_gold() -> None:
    eng = _engine(family_fusion=True)
    await eng.start()
    try:
        for i in range(40):
            await eng.write(
                f"garden travel plans note {i} garden travel garden travel",
                namespace="a",
                memory_type="episodic",
            )
        await eng.write(
            f"a {_GOLD} appeared once between many other unrelated words today",
            namespace="a",
            memory_type="episodic",
        )
        gold_id = next(r.record_id for r in await eng._records("a") if _GOLD in r.content)
        with search_forensics() as fx:
            await eng.search(f"garden travel garden travel {_GOLD}", namespace="a", top_k=5)
        assert any(rid == gold_id for rid, _ in fx["pool"])
        assert fx["protected"][gold_id] == "lexical"
    finally:
        await eng.stop()


async def _derived_store(eng: Engine) -> tuple[str, str]:
    turn = await eng.write(
        "Caroline said she adopted a dog named Biscuit in March",
        namespace="a",
        memory_type="episodic",
    )
    turn_id = turn.record_id if hasattr(turn, "record_id") else str(turn)
    fact = await eng.write(
        "Caroline adopted a dog named Biscuit",
        namespace="a",
        memory_type="semantic",
        derived_from=[turn_id],
    )
    fact_id = fact.record_id if hasattr(fact, "record_id") else str(fact)
    return turn_id, fact_id


async def test_lineage_adapter_reads_source_parents() -> None:
    eng = _engine()
    await eng.start()
    try:
        turn_id, fact_id = await _derived_store(eng)
        fact = await eng._require_started().get_record(fact_id)
        assert fact is not None and fact.source.parents == [turn_id]
        lineage, fetched = await lineage_from_records(eng, [fact], 4)
        assert [(e.child, e.parent, e.role) for e in lineage[fact_id]] == [
            (fact_id, turn_id, "support")
        ]
        assert fetched[turn_id].memory_type == "episodic"
    finally:
        await eng.stop()


async def test_lift_before_rerank_replaces_fact_by_turn() -> None:
    eng = _engine(lift_before_rerank=True)
    await eng.start()
    try:
        turn_id, fact_id = await _derived_store(eng)
        with search_forensics() as fx:
            out = await eng.search("Caroline dog Biscuit", namespace="a", top_k=5)
        ids = [r.record_id for r, _ in out]
        assert turn_id in ids
        assert fact_id not in ids
        assert fx["lift"]["dropped"] == []
    finally:
        await eng.stop()


async def test_lift_off_keeps_fact() -> None:
    eng = _engine()
    await eng.start()
    try:
        _, fact_id = await _derived_store(eng)
        out = await eng.search("Caroline dog Biscuit", namespace="a", top_k=5)
        assert fact_id in [r.record_id for r, _ in out]
    finally:
        await eng.stop()


class _Fake:
    def __init__(self, rid: str, scores: list[float | None] | None = None) -> None:
        self.reranker_id = rid
        self._scores = scores

    async def rerank(self, query: str, documents: list[str]) -> list[Any]:
        if self._scores is not None:
            return self._scores[: len(documents)]
        return [float(len(d)) for d in documents]


async def _rerank_run(cache: dict[str, Any], **rerank: Any) -> tuple[list[str], dict[str, Any]]:
    eng = _engine({"chain": _V2_CHAIN, **rerank})
    await eng.start()
    try:
        for i in range(8):
            await eng.write(f"note {i} " + "word " * i, namespace="a", memory_type="episodic")
        # ADR-082: the first chain entry is the engine's own reranker; the others are built
        # once per (provider, settings, device) into the chain cache.
        eng.__dict__["_rerank_chain_cache"] = {
            (name, "{}", None): svc for name, svc in cache.items() if name != "jina"
        }

        async def first() -> Any:
            return cache.get("jina")

        eng.__dict__["_rerank_provider"] = first
        with search_forensics() as fx:
            out = await eng.search("note word", namespace="a", top_k=8)
        return [r.record_id for r, _ in out], fx
    finally:
        await eng.stop()


async def test_rerank_v2_chain_falls_back_and_never_scores_zero() -> None:
    cache = {"jina": None, "qwen3": _Fake("qwen3:fake"), "onnx": _Fake("minilm:fake")}
    _, fx = await _rerank_run(cache)
    v2 = fx["rerank_v2"]
    assert v2["model"] == "qwen3:fake"
    assert v2["fallback_used"] == "qwen3"
    assert v2["tried"] == [{"mode": "jina", "outcome": "unavailable"}]
    assert v2["unscored"] == []


async def test_rerank_v2_pool_cap_leaves_tail_unscored() -> None:
    cache = {"jina": _Fake("jina:fake"), "qwen3": None, "onnx": None}
    ids, fx = await _rerank_run(cache, pool=3)
    v2 = fx["rerank_v2"]
    assert v2["scored"] == 3 and len(v2["unscored"]) == 5
    assert len(ids) == 8  # unscored candidates stay in the result, after the scored ones
    scored_ids = {rid for rid, _ in fx["rerank_scores"]}
    assert not scored_ids & set(v2["unscored"])


async def test_rerank_v2_none_scores_are_unscored() -> None:
    cache = {"jina": _Fake("jina:fake", [1.0, None, 2.0, None, 3.0, None, 4.0, None])}
    _, fx = await _rerank_run(cache)
    assert fx["rerank_v2"]["scored"] == 4 and len(fx["rerank_v2"]["unscored"]) == 4


async def test_rerank_v2_all_unavailable_is_unscored() -> None:
    cache = {"jina": None, "qwen3": None, "onnx": None}
    ids, fx = await _rerank_run(cache)
    assert fx["rerank_v2"]["model"] is None and fx["rerank_v2"]["scored"] == 0
    assert len(ids) == 8


def test_jina_tail_is_none_when_flag_on() -> None:
    class _Model:
        def rerank(self, query: str, docs: list[str]) -> list[dict[str, Any]]:
            return [{"index": i, "relevance_score": 1.0 - i * 0.1} for i in range(len(docs))]

    for flag, tail in ((False, 0.0), (True, None)):
        rr = JinaReranker.__new__(JinaReranker)
        rr._model = _Model()
        rr._max_documents = 2
        rr._infer_lock = __import__("threading").Lock()
        rr.unscored_tail = flag
        out = rr.score("q", ["a", "b", "c", "d"])
        assert out[:2] == [1.0, 0.9]
        assert out[2:] == [tail, tail]
