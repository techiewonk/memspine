"""G5b: ``read.rerank_keep`` caps what a reranked wide pool passes to assembly."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine


class _Reranker:
    reranker_id = "fake"

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.seen = 0

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        self.seen = len(documents)
        if self.fail:
            raise RuntimeError("reranker down")
        return [float(i) for i in range(len(documents))]  # reverses the order


def _engine(**read: Any) -> Engine:
    return Engine(
        template="base",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _assemble(eng: Engine, reranker: _Reranker | None) -> list[str]:
    for i in range(20):
        await eng.write(f"note {i} about the garden and the tomatoes", namespace="a")
    eng._rerank_provider = lambda: reranker  # type: ignore[method-assign]
    out = await eng.assemble("garden tomatoes", namespace="a", budget_tokens=4000, top_k=5)
    return [r.content for r in out.records]


@pytest.mark.parametrize(
    ("read", "with_reranker", "fail", "expected", "pool_seen"),
    [
        ({"candidate_pool": 3, "rerank_keep": 4}, True, False, 4, 15),
        ({"candidate_pool": 3}, True, False, 15, 15),  # unset: the whole pool
        ({"candidate_pool": 1, "rerank_keep": 2}, True, False, 5, 5),  # no wide pool
        ({"candidate_pool": 3, "rerank_keep": 4}, False, False, 15, 0),  # no reranker
        ({"candidate_pool": 3, "rerank_keep": 4}, True, True, 15, 15),  # rerank failed
    ],
)
async def test_rerank_keep(
    read: dict[str, Any], with_reranker: bool, fail: bool, expected: int, pool_seen: int
) -> None:
    eng = _engine(**read)
    await eng.start()
    try:
        reranker = _Reranker(fail) if with_reranker else None
        records = await _assemble(eng, reranker)
        assert len(records) == expected
        assert (reranker.seen if reranker else 0) == pool_seen
    finally:
        await eng.stop()


async def test_kept_records_are_the_reranker_best() -> None:
    eng = _engine(candidate_pool=3, rerank_keep=3)
    await eng.start()
    try:
        reranker = _Reranker()
        captured: list[list[str]] = []
        real = reranker.rerank

        async def spy(query: str, documents: list[str]) -> list[float]:
            captured.append(documents)
            return await real(query, documents)

        reranker.rerank = spy  # type: ignore[method-assign]
        records = await _assemble(eng, reranker)
        last_three = captured[0][-3:]  # highest reranker scores (documents carry a prefix)
        assert len(records) == 3
        assert all(any(doc.endswith(r) for doc in last_three) for r in records)
    finally:
        await eng.stop()
