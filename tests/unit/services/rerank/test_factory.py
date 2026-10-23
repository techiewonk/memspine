"""RerankerFactory (A2/D-51): registry-driven modes, graceful-degrade, register seam."""

from __future__ import annotations

import pytest

from memspine.exceptions import ConfigError
from memspine.providers.contracts import BuildResult, ProviderRef
from memspine.providers.registry import ProviderDescriptor, get_registry
from memspine.providers.rerank import (
    RerankSettings,
    build_reranker,
    register_reranker,
    rerank_modes,
)
from memspine.providers.settings import EmptySettings


def test_rerank_modes_is_off_plus_registry() -> None:
    modes = rerank_modes()
    assert modes[0] == "off"
    assert {"onnx", "flashrank", "litellm"} <= set(modes)  # the shipped providers


async def test_off_builds_none() -> None:
    assert await build_reranker(RerankSettings(mode="off")) is None


async def test_unknown_mode_fails_explicitly() -> None:
    with pytest.raises(ConfigError, match="unknown reranker provider"):
        await build_reranker(RerankSettings(mode="bogus"))


async def test_litellm_without_model_fails_typed_settings_validation() -> None:
    with pytest.raises(ConfigError, match=r"invalid model\.rerank\.chain\[\]\.settings"):
        await build_reranker(
            RerankSettings(mode="litellm", model=None), explicit_deployment_id=True
        )


async def test_register_reranker_extends_modes_and_builds() -> None:
    class _FakeReranker:
        reranker_id = "fake"

        async def rerank(self, query: str, documents: list[str]) -> list[float]:
            return [0.0] * len(documents)

    class Builder:
        async def build(self, settings: EmptySettings, ctx: object) -> BuildResult[_FakeReranker]:
            return BuildResult(
                _FakeReranker(), ProviderRef("rerank", "fake_test"), (), frozenset({"rerank"})
            )

    register_reranker(
        ProviderDescriptor(
            ProviderRef("rerank", "fake_test"),
            EmptySettings,
            frozenset({"rerank"}),
            (),
            Builder(),
        )
    )
    try:
        assert "fake_test" in rerank_modes()
        built = await build_reranker(RerankSettings(mode="fake_test"))
        assert built is not None and built.reranker_id == "fake"
    finally:
        get_registry("rerankers").unregister("fake_test")  # don't leak into other tests
