"""GR-15 fix: ``read.rerank_balanced`` must actually widen what the reranker can choose from.

Before the fix the fused list was cut to the pool size before the balanced selection, so the
option was a no-op (identical answers on 233 dev questions, 2026-10-10)."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.engine import search_forensics
from memspine.services.rerank.factory import RerankSettings, register_reranker


class _ConstReranker:
    reranker_id = "const"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [1.0 / (i + 1) for i in range(len(documents))]


def _const(settings: RerankSettings) -> Any:
    return _ConstReranker()


register_reranker("const_test", _const)


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"record_access": False, "rerank": "const_test", **read},
    )


@pytest.mark.parametrize(("balanced", "factor"), [(False, 1), (True, 3)])
async def test_balanced_rerank_keeps_a_wider_fused_list(balanced: bool, factor: int) -> None:
    eng = _engine(rerank_balanced=balanced)
    await eng.start()
    try:
        for i in range(80):
            await eng.write(
                f"note {i} about garden plants and travel plans",
                namespace="a",
                memory_type="episodic",
            )
        with search_forensics() as stages:
            await eng.search("garden travel", namespace="a", top_k=5)
        assert len(stages["fused"]) == 5 * factor
        assert len(stages["pool"]) <= 5 * factor
        assert stages["reranker"] == "const"
    finally:
        await eng.stop()
