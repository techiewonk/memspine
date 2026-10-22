"""FX-READPATH: read settings found inert on the eval read path (Engine.read, replay).

Model-free: a lexical-overlap stub replaces the reranker chain, the hash embedder stands in for
the encoder. Invented names only.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import memspine.read.pipeline.rerank as rerank_mod
from memspine import Engine
from memspine.observability.trace import search_forensics
from memspine.store.records import MemoryRecord

_QUERY = "where does Ilse keep the spare key"
_T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _lexical_reranker(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    sizes: list[int] = []

    async def lexical(eng: Any, query: str, documents: list[str], cfg: Any) -> Any:
        sizes.append(len(documents))
        q = set(query.lower().split())
        return [float(len(q & set(d.lower().split()))) for d in documents], "stub:lexical", None, []

    monkeypatch.setattr(rerank_mod, "chain_score", lexical)
    return sizes


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
        embedding={"provider": "hash", "settings": {}},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "rerank": "off", **read},
    )


async def _fill(eng: Engine, n: int = 60) -> None:
    for i in range(n):
        await eng.write(
            f"where does where does the neighbour keep the cat {i} where does keep the",
            namespace="a",
            memory_type="episodic",
        )
    await eng.write(
        "Ilse will keep the spare key under the blue flowerpot",
        namespace="a",
        memory_type="episodic",
    )


async def _rerank_sizes(sizes: list[int], **read: Any) -> list[int]:
    eng = _engine(**read)
    await eng.start()
    try:
        await _fill(eng)
        sizes.clear()
        await eng.read(_QUERY, namespace="a", mode="replay", top_k=2)
        return list(sizes)
    finally:
        await eng.stop()


# ------------------------------------------------------------------ S67: rerank_v2_search_pool


async def test_read_replay_widens_the_rerank_pool(_lexical_reranker: list[int]) -> None:
    off = await _rerank_sizes(_lexical_reranker, rerank_v2=True)
    zero = await _rerank_sizes(_lexical_reranker, rerank_v2=True, rerank_v2_search_pool=0)
    on = await _rerank_sizes(_lexical_reranker, rerank_v2=True, rerank_v2_search_pool=20)
    assert off and off == zero
    assert max(off) <= 2
    assert max(on) > max(off) and max(on) <= 20


async def test_candidate_pool_is_not_widened_twice(_lexical_reranker: list[int]) -> None:
    # want = top_k x candidate_pool = 10 > search_pool 5: the key changes nothing
    base = await _rerank_sizes(_lexical_reranker, rerank_v2=True, candidate_pool=5)
    small = await _rerank_sizes(
        _lexical_reranker, rerank_v2=True, candidate_pool=5, rerank_v2_search_pool=5
    )
    assert base == small


# ------------------------------------------------------------------ S10 / S36: tiers_v2


async def test_tier_sets_the_rerank_pool(_lexical_reranker: list[int]) -> None:
    off = await _rerank_sizes(_lexical_reranker, rerank_v2=True)
    fast = await _rerank_sizes(_lexical_reranker, rerank_v2=True, tiers_v2=True)
    assert max(off) <= 2
    assert max(fast) == 32  # the fast tier's pool (read/tiers.py DEFAULT_TIER_TABLE)
    standard = await _rerank_sizes(
        _lexical_reranker, rerank_v2=True, tiers_v2=True, tiers_v2_depth="standard"
    )
    assert max(standard) == 48


async def test_tier_without_generative_calls_skips_the_deep_model_step() -> None:
    stops = []
    for tiers in (False, True):
        eng = _engine(deep_v2=True, obligations=True, tiers_v2=tiers)
        await eng.start()
        try:
            await eng.write("Bram likes tea", namespace="a", memory_type="episodic")
            with search_forensics() as fx:
                await eng.read("where does Noor live", namespace="a", mode="replay", top_k=2)
            info = fx.get("deep_v2")
            stops.append(info["stop"] if info else None)
        finally:
            await eng.stop()
    assert stops[1] == "tier_no_generative_calls"
    assert stops[0] != "tier_no_generative_calls"


# ---- S19 / S63: neighbours_v2 in replay

_TURNS = [
    "Bram bought a green bicycle on Monday",
    "Noor asked about the price of the bicycle",
    "ok",
    "Bram said the bicycle cost two hundred euros",
    "haha",
    "Noor wants to buy the same bicycle next week",
    "thanks",
    "Bram will show Noor the shop on Friday",
    "Noor brought her sister Ilse along",
    "They all went home at six",
]


async def _replay_ids(monkeypatch: pytest.MonkeyPatch, **read: Any) -> list[int]:
    eng = _engine(**read)
    await eng.start()
    try:
        turns: list[MemoryRecord] = [
            await eng.write(
                text,
                namespace="a",
                memory_type="episodic",
                valid_from=_T0 + timedelta(minutes=i),
            )
            for i, text in enumerate(_TURNS)
        ]

        async def fake_search(*_a: Any, **_k: Any) -> list[tuple[MemoryRecord, float]]:
            return [(turns[5], 0.9)]

        monkeypatch.setattr(eng, "_search", fake_search)
        out = await eng.read(
            "when does Noor want the bicycle",
            namespace="a",
            mode="replay",
            top_k=1,
            replay_window=2,
        )
        index = {t.record_id: i for i, t in enumerate(turns)}
        return sorted(index[r.record_id] for r in out.context.records if r.record_id in index)
    finally:
        await eng.stop()


async def test_neighbours_v2_replay_window_skips_filler(monkeypatch: pytest.MonkeyPatch) -> None:
    off = await _replay_ids(monkeypatch)
    on = await _replay_ids(monkeypatch, neighbours_v2=True)
    assert off == [3, 4, 5, 6, 7]  # fixed -2/+2, filler included
    assert on == [1, 3, 5, 7, 8]  # filler 4 and 6 take no slot; the reach stretches over them
