"""ONNX connector contracts without network or heavyweight sessions."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.clients.weights.onnx import OnnxModel, text_weights
from memspine.clients.weights.onnx_catalogue import (
    CATALOGUE,
    legacy_embedder_id,
    model_embedder_id,
    model_reranker_id,
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
    assert first.embedder_id == f"embedder:{MODEL}"
    assert first.legacy_embedder_id == f"fastembed:{MODEL}"
    assert second.legacy_embedder_id is None
    assert first.embedder_id != second.embedder_id
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


def test_identity_matches_adapter_and_cannot_impersonate_certified_model() -> None:
    from dataclasses import asdict, replace

    spec = CATALOGUE[MODEL]
    values = asdict(spec)
    assert not resolve_model_spec(MODEL, spec).certified
    assert model_embedder_id(MODEL) == OnnxEmbedding(MODEL).embedder_id
    assert model_embedder_id(MODEL, spec).startswith(f"onnx:{MODEL}:")
    assert model_embedder_id(MODEL, values) == model_embedder_id(MODEL, spec)
    identities = {
        model_embedder_id(MODEL, spec),
        model_embedder_id(MODEL, replace(spec, pooling="mean")),
        model_embedder_id(MODEL, replace(spec, revision="0" * 40)),
        model_embedder_id(MODEL, replace(spec, passage_prefix="passage: ")),
        model_embedder_id(MODEL, replace(spec, query_prefix="query: ")),
        model_embedder_id(MODEL, replace(spec, normalize=False)),
    }
    assert len(identities) == 6
    instance = OnnxEmbedding(MODEL, model_spec=values)
    frozen_id = instance.embedder_id
    values["passage_prefix"] = "mutated"
    assert instance.embedder_id == frozen_id
    reranker = CATALOGUE["Xenova/ms-marco-MiniLM-L-6-v2"]
    assert model_reranker_id("custom", reranker) != model_reranker_id(
        "custom", replace(reranker, revision="0" * 40)
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("dim", True),
        ("max_length", 1.5),
        ("normalize", "true"),
        ("pooling", "guessed"),
        ("model_file", "../escape.onnx"),
        ("model_file", "C:/escape.onnx"),
        ("tokenizer_file", "/tokenizer.json"),
        ("additional_files", "model.onnx_data"),
        ("revision", "main"),
        ("passage_prefix", None),
    ],
)
def test_custom_model_metadata_rejects_ambiguous_values(field: str, value: Any) -> None:
    from dataclasses import asdict

    spec = asdict(CATALOGUE[MODEL])
    spec[field] = value
    with pytest.raises(ValueError, match="ONNX"):
        resolve_model_spec(MODEL, spec)


def test_provider_policy_frozen_and_checked_before_load() -> None:
    providers = ["CPUExecutionProvider"]
    weights = text_weights(MODEL, providers=providers)
    identity = weights.identity()
    providers.append("mutated")
    assert weights.identity() == identity
    assert identity == text_weights(MODEL, providers=("CPUExecutionProvider",)).identity()
    with pytest.raises(ValueError, match="threads"):
        text_weights(MODEL, threads=True)
    with pytest.raises(ValueError, match="providers"):
        text_weights(MODEL, providers=[])


def test_query_instruction_changes_projection_identity_without_changing_shared_weights() -> None:
    from dataclasses import replace

    default = OnnxEmbedding(MODEL)
    override = OnnxEmbedding(MODEL, query_instruction="query: ")
    assert override.embedder_id.startswith(f"onnx:{MODEL}:")
    assert override.embedder_id == model_embedder_id(MODEL, query_instruction="query: ")
    assert override.embedder_id != model_embedder_id(MODEL, query_instruction="different: ")
    assert default.embedder_id == model_embedder_id(MODEL, query_instruction="")
    assert override._weights.identity() == default._weights.identity()
    # A spec with the same effective prefix has the same custom identity, and
    # overriding that prefix changes it again.
    spec = replace(CATALOGUE[MODEL], query_prefix="query: ")
    assert model_embedder_id(MODEL, spec) == override.embedder_id
    assert model_embedder_id(MODEL, spec, query_instruction="different: ") != override.embedder_id
