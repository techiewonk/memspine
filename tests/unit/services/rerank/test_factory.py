"""RerankerFactory (A2/D-51): registry-driven modes, graceful-degrade, register seam."""

from __future__ import annotations

from memspine.services.rerank import factory
from memspine.services.rerank.factory import (
    RerankSettings,
    build_reranker,
    register_reranker,
    rerank_modes,
)


def test_rerank_modes_is_off_plus_registry() -> None:
    modes = rerank_modes()
    assert modes[0] == "off"
    assert {"fastembed", "flashrank", "litellm"} <= set(modes)  # the shipped providers


def test_off_builds_none() -> None:
    assert build_reranker(RerankSettings(mode="off")) is None


def test_unknown_mode_builds_none() -> None:
    assert build_reranker(RerankSettings(mode="bogus")) is None


def test_litellm_without_model_degrades_to_none() -> None:
    # missing model → MissingServiceError inside the spec → swallowed to None
    # (no litellm import, no network) — the sticky-skip contract.
    assert build_reranker(RerankSettings(mode="litellm", model=None)) is None


def test_register_reranker_extends_modes_and_builds() -> None:
    class _FakeReranker:
        reranker_id = "fake"

        async def rerank(self, query: str, documents: list[str]) -> list[float]:
            return [0.0] * len(documents)

    register_reranker("fake_test", lambda s: _FakeReranker())  # type: ignore[arg-type,return-value]
    try:
        assert "fake_test" in rerank_modes()
        built = build_reranker(RerankSettings(mode="fake_test"))
        assert built is not None and built.reranker_id == "fake"
    finally:
        factory._REGISTRY.pop("fake_test", None)  # don't leak into other tests
