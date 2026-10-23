"""GR-15 fix: ``model.rerank.balanced`` must actually widen what the reranker can choose from.

Before the fix the fused list was cut to the pool size before the balanced selection, so the
option was a no-op (identical answers on 233 dev questions, 2026-10-10)."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.observability.trace import search_forensics
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import ProviderDescriptor, get_registry
from memspine.providers.rerank import register_reranker
from memspine.providers.settings import EmptySettings


class _ConstReranker:
    reranker_id = "const"

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        return [1.0 / (i + 1) for i in range(len(documents))]


class _ConstBuilder:
    def construct(self, settings: EmptySettings, ctx: Any = None) -> _ConstReranker:
        return _ConstReranker()

    async def build(self, settings: EmptySettings, ctx: Any) -> BuildResult[_ConstReranker]:
        return BuildResult(
            self.construct(settings),
            ProviderRef("rerank", "const_test"),
            (),
            frozenset({"rerank"}),
        )


@pytest.fixture(scope="module", autouse=True)
def _const_provider():
    register_reranker(
        ProviderDescriptor(
            ProviderRef("rerank", "const_test"),
            EmptySettings,
            frozenset({"rerank"}),
            (),
            _ConstBuilder(),
        )
    )
    yield
    get_registry("rerankers").unregister("const_test")


def _engine(**rerank: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "rerank": {"chain": [{"provider": "const_test", "settings": {}}], **rerank},
        },
        memories={"episodic": {"enabled": True}},
        read={"record_access": False},
    )


@pytest.mark.parametrize(("balanced", "factor"), [(False, 1), (True, 3)])
async def test_balanced_rerank_keeps_a_wider_fused_list(balanced: bool, factor: int) -> None:
    eng = _engine(balanced=balanced)
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
