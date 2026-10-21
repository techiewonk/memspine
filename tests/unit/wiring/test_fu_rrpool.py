"""FU-RRPOOL: ``read.rerank_v2_search_pool`` widens what ``search`` hands the v2 reranker.

Model-free: a lexical-overlap stub replaces the reranker chain, the hash embedder stands in for the
encoder. INV-FILLER shape: filler turns share the question words and outrank the evidence in
the fused order, so a top_k-wide pool never shows the evidence to the reranker.
"""

from __future__ import annotations

from typing import Any

import pytest

import memspine.read.pipeline.rerank as rerank_mod
from memspine import Engine
from memspine.config.schema import MemspineConfig, ReadConfig
from memspine.exceptions import ConfigError

_QUERY = "where does Ilse keep the spare key"
_EVIDENCE = "Ilse will keep the spare key under the blue flowerpot"
_FILLERS = 24


@pytest.fixture(autouse=True)
def _lexical_reranker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    sizes: list[int] = []

    async def lexical(eng: Any, query: str, documents: list[str], cfg: Any) -> Any:
        sizes.append(len(documents))
        q = set(query.lower().split())
        scores = [float(len(q & set(d.lower().split()))) for d in documents]
        return scores, "stub:lexical", None, []

    monkeypatch.setattr(rerank_mod, "chain_score", lexical)
    return sizes


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"provider": "sqlite", "settings": {"path": ":memory:", "busy_timeout_s": 5.0}},
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "rerank": "off", **read},
    )


async def _fill(eng: Engine) -> str:
    for i in range(_FILLERS):
        await eng.write(
            f"where does where does the neighbour keep the cat {i} where does keep the",
            namespace="a",
            memory_type="episodic",
        )
    await eng.write(_EVIDENCE, namespace="a", memory_type="episodic")
    return next(r.record_id for r in await eng._records("a") if r.content == _EVIDENCE)


async def _top(eng: Engine, top_k: int = 2) -> list[str]:
    return [r.record_id for r, _ in await eng.search(_QUERY, namespace="a", top_k=top_k)]


def test_default_off_and_requires() -> None:
    assert ReadConfig().rerank_v2_search_pool == 0
    runtime = {
        "provider_services": {
            "health_timeout_s": 1.0,
            "close_timeout_s": 1.0,
            "startup_timeout_s": None,
        }
    }
    with pytest.raises(ConfigError, match="rerank_v2"):
        MemspineConfig(read={"rerank_v2_search_pool": 20}, runtime=runtime)
    MemspineConfig(read={"rerank_v2": True, "rerank_v2_search_pool": 20}, runtime=runtime)


async def test_narrow_pool_loses_the_evidence() -> None:
    eng = _engine(rerank_v2=True)
    await eng.start()
    try:
        gold = await _fill(eng)
        assert gold not in await _top(eng)
    finally:
        await eng.stop()


async def test_wide_pool_keeps_the_evidence_in_top_2(_lexical_reranker: list[int]) -> None:
    eng = _engine(rerank_v2=True, rerank_v2_search_pool=20)
    await eng.start()
    try:
        gold = await _fill(eng)
        out = await _top(eng)
        assert len(out) == 2 and gold in out
        assert _lexical_reranker[-1] > 2  # the reranker saw the wide pool
    finally:
        await eng.stop()


async def test_flag_off_matches_default() -> None:
    plain = _engine(rerank_v2=True)
    zero = _engine(rerank_v2=True, rerank_v2_search_pool=0)
    outs = []
    for eng in (plain, zero):
        await eng.start()
        try:
            await _fill(eng)
            outs.append([r.content for r, _ in await eng.search(_QUERY, namespace="a", top_k=5)])
        finally:
            await eng.stop()
    assert outs[0] == outs[1]


async def test_assemble_honours_the_key(_lexical_reranker: list[int]) -> None:
    # FX-READPATH: assemble / read widen through the same key; key off = unchanged.
    sizes = []
    for pool in (0, 20):
        eng = _engine(rerank_v2=True, rerank_v2_search_pool=pool)
        await eng.start()
        try:
            await _fill(eng)
            _lexical_reranker.clear()
            await eng.assemble(_QUERY, namespace="a", top_k=2)
            sizes.append(list(_lexical_reranker))
        finally:
            await eng.stop()
    assert sizes[0] and max(sizes[0]) <= 2
    assert max(sizes[1]) > max(sizes[0])
