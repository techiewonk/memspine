"""RETRIEVAL_GAPS fixes: asymmetric replay window, ``read.rerank_floor``, wide rerank pool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from memspine import Engine
from memspine.config.schema import ReadConfig
from memspine.core.records import MemoryRecord

WORDS = [f"w{i}x{i * 7}" for i in range(60)]
T0 = datetime(2023, 5, 7, 10, 0, tzinfo=UTC)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _session(eng: Engine, monkeypatch: pytest.MonkeyPatch) -> list[MemoryRecord]:
    turns = [
        await eng.write(
            f"turn {i} of the talk",
            namespace="a",
            memory_type="episodic",
            valid_from=T0 + timedelta(minutes=i),
        )
        for i in range(9)
    ]
    hit = turns[3]

    async def fake_search(
        probe: str, namespace: str = "a", top_k: int = 8, **_: Any
    ) -> list[tuple[MemoryRecord, float]]:
        return [(hit, 0.9)]

    monkeypatch.setattr(eng, "_search", fake_search)
    return turns


@pytest.mark.parametrize("mode", ["compose", "replay"])
@pytest.mark.parametrize(
    ("read", "lo", "hi"),
    [
        ({}, 1, 5),  # unset: the +-2 window of the read argument
        ({"replay_window_before": 1, "replay_window_after": 4}, 2, 7),
        ({"replay_window_before": 0}, 3, 5),  # one side only; the other keeps 2
        ({"replay_window_after": 0}, 1, 3),
    ],
)
async def test_window_sides(
    mode: str, read: dict[str, Any], lo: int, hi: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    eng = _engine(compose_replay=True, **read)
    await eng.start()
    try:
        turns = await _session(eng, monkeypatch)
        out = await eng.read("how many turns", namespace="a", mode=mode, top_k=1, replay_window=2)
        got = [r.record_id for r in out.context.records]
        assert got == [t.record_id for t in turns[lo : hi + 1]]
    finally:
        await eng.stop()


def test_window_sides_validate() -> None:
    with pytest.raises(ValidationError):
        ReadConfig(replay_window_after=-1)
    assert ReadConfig().replay_window_before is None
    assert ReadConfig().replay_window_after is None


class _Reranker:
    reranker_id = "fake"

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.seen = documents
        return [float(len(documents) - i) for i in range(len(documents))]


async def _read(read: dict[str, Any], reranker: _Reranker | None, top_k: int = 5) -> list[str]:
    eng = _engine(**read)
    await eng.start()
    try:
        for i in range(60):
            await eng.write(f"note {i} about the garden and the tomatoes {WORDS[i]}", namespace="a")
        eng._rerank_provider = lambda: reranker  # type: ignore[method-assign]
        out = await eng.assemble("garden tomatoes", namespace="a", budget_tokens=4000, top_k=top_k)
        return [r.content for r in out.records]
    finally:
        await eng.stop()


async def test_rerank_floor_skip_keeps_reranked_hits_the_minmax_floor_drops() -> None:
    floor = {"assembly": {"relative_floor": 0.9}}
    base = {"candidate_pool": 3, **floor}
    minmax = await _read(base, _Reranker())
    skip = await _read({**base, "rerank_floor": "skip"}, _Reranker())
    assert len(minmax) < 15  # min-max puts the worst at 0.0: the floor deletes hits
    assert len(skip) == 15  # nothing deleted by the floor
    assert set(minmax) <= set(skip)


async def test_rerank_floor_skip_still_floors_a_read_the_reranker_did_not_score() -> None:
    floor = {"assembly": {"relative_floor": 0.99}, "candidate_pool": 3}
    plain = await _read(floor, None)
    skip = await _read({**floor, "rerank_floor": "skip"}, None)
    assert skip == plain
    assert len(plain) < 15


def test_default_rerank_floor_is_minmax() -> None:
    assert ReadConfig().rerank_floor == "minmax"


@pytest.mark.parametrize("pool", [2, 3])
async def test_wider_pool_reaches_the_reranker_and_only_rerank_keep_goes_on(pool: int) -> None:
    reranker = _Reranker()
    records = await _read({"candidate_pool": pool, "rerank_keep": 10}, reranker, top_k=10)
    assert len(reranker.seen) == 10 * pool  # the reranker sees top_k x pool
    assert len(records) == 10  # and only rerank_keep of them leave
    top = reranker.seen[:10]  # the fake reranker ranks its input order
    assert all(any(doc.endswith(r) for doc in top) for r in records)
