"""ONNX connector contracts without network or heavyweight sessions."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.clients.weights.onnx import OnnxModel, text_weights
from memspine.clients.weights.onnx_catalogue import (
    CATALOGUE,
    legacy_embedder_id,
    resolve_model_spec,
)
from memspine.clients.weights.shared import PrivateWeightRegistry
from memspine.services.embedding.onnx import OnnxEmbedding

MODEL = "BAAI/bge-small-en-v1.5"


def test_offline_missing_file_names_file(monkeypatch: pytest.MonkeyPatch) -> None:
    import huggingface_hub

    def missing(repo: str, filename: str, **kwargs: Any) -> str:
        assert kwargs["local_files_only"] is True
        assert kwargs["revision"] == CATALOGUE[MODEL].revision
        raise RuntimeError("not cached")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", missing)
    with pytest.raises(FileNotFoundError, match="model_optimized.onnx"):
        OnnxModel(CATALOGUE[MODEL], local_files_only=True)


def test_catalogue_and_custom_certification() -> None:
    from dataclasses import asdict

    assert legacy_embedder_id(MODEL) == f"fastembed:{MODEL}"
    assert legacy_embedder_id(MODEL, query_instruction="query: ") is None
    values = asdict(CATALOGUE[MODEL])
    assert not resolve_model_spec("custom", values).certified
    assert legacy_embedder_id(MODEL, model_spec=values) is None
    assert OnnxEmbedding(MODEL, model_spec=values).embedder_id.startswith("onnx:")
    with pytest.raises(ValueError, match="explicit"):
        resolve_model_spec("unknown")
    with pytest.raises(ValueError, match="missing explicit"):
        resolve_model_spec("unknown", {"repo": "missing"})


@pytest.mark.asyncio
async def test_identical_specs_share_one_load_without_sharing_holder_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import memspine.clients.weights.onnx as module

    loads = 0

    class FakeModel:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal loads
            loads += 1

        def embed(self, texts: list[str], *, batch_size: int = 256) -> list[list[float]]:
            return [[float(len(text))] * 384 for text in texts]

    monkeypatch.setattr(module, "OnnxModel", FakeModel)
    registry = PrivateWeightRegistry()
    first_weights = text_weights(MODEL, registry=registry)
    second_weights = text_weights(MODEL, registry=registry)
    first = OnnxEmbedding(MODEL, weights=first_weights, batch_size=1)
    second = OnnxEmbedding(MODEL, weights=second_weights, batch_size=5, query_instruction="query: ")
    assert (await first.embed(["abc"]))[0][0] == 3
    assert (await second.embed_queries(["abc"]))[0][0] == 10
    assert loads == 1
    assert first.embedder_id == f"embed:{MODEL}"
    assert first.legacy_embedder_id == f"fastembed:{MODEL}"
    assert second.legacy_embedder_id is None
    await first.aclose()
    assert (await second.embed(["x"]))[0][0] == 1
    await second.aclose()


def test_weight_identity_covers_runtime_policy() -> None:
    first = text_weights(MODEL, threads=1)
    assert first.identity() != text_weights(MODEL, threads=2).identity()
    assert first.identity() != text_weights(MODEL, threads=1, local_files_only=True).identity()


@pytest.mark.asyncio
async def test_empty_embedding_never_loads() -> None:
    instance = OnnxEmbedding()
    assert await instance.embed([]) == []
    assert not instance._weights.loaded
    await instance.aclose()
