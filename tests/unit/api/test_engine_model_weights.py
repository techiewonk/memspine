"""ADR-093 / D-10: a cold weight cache fails Engine start naming the fetch command."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memspine import Engine
from memspine.exceptions import MissingServiceError

_STORAGE = {"records": {"provider": "sqlite", "settings": {"path": ":memory:"}}}


@pytest.fixture
def cold_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty Hugging Face cache, offline: any download attempt fails the test."""
    import huggingface_hub
    import huggingface_hub.constants

    cache = tmp_path / "hf"
    cache.mkdir()
    monkeypatch.setattr(huggingface_hub.constants, "HF_HUB_CACHE", str(cache))
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")

    def no_download(*args: Any, **kwargs: Any) -> str:
        raise AssertionError("Engine start must never download weights")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", no_download)
    return cache


def _engine(**overrides: Any) -> Engine:
    return Engine(template="core", dotenv_path=None, storage=_STORAGE, **overrides)


async def test_cold_cache_embedder_start_names_the_fetch_command(cold_cache: Path) -> None:
    engine = _engine()  # model.embedding defaults to onnx BAAI/bge-small-en-v1.5
    with pytest.raises(MissingServiceError) as err:
        await engine.start()
    message = str(err.value)
    assert "model.embedding" in message
    assert "memspine models fetch --model BAAI/bge-small-en-v1.5" in message


async def test_cold_cache_reranker_start_names_the_fetch_command(cold_cache: Path) -> None:
    engine = _engine(
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "rerank": {"chain": [{"provider": "onnx", "settings": {}}]},
        }
    )
    with pytest.raises(MissingServiceError, match="memspine models fetch") as err:
        await engine.start()
    assert "model.rerank.chain[0]" in str(err.value)


async def test_non_strict_cold_reranker_is_skipped(cold_cache: Path) -> None:
    engine = _engine(
        strict_services=False,
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "rerank": {"chain": [{"provider": "onnx", "settings": {}}]},
        },
    )
    await engine.start()
    try:
        assert await engine._rerank_provider() is None  # skipped, never downloaded
        await engine.write("a note about the garden", namespace="a")
        assert await engine.search("garden", namespace="a", top_k=3)
    finally:
        await engine.stop()


async def test_explicit_download_setting_skips_the_start_check(cold_cache: Path) -> None:
    engine = _engine(
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "rerank": {"chain": [{"provider": "onnx", "settings": {"download": True}}]},
        },
        runtime={"shared_resources": {"warmup": "lazy"}},
    )
    await engine.start()  # download: true defers to the load; nothing is fetched at start
    await engine.stop()
