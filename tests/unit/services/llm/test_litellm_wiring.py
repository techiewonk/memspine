"""LiteLLM embedding + rerank adapters and engine prefix-routing (Phase 4)."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.services.embedding.litellm_embed import LiteLLMEmbedding
from memspine.services.llm.litellm_llm import LiteLLMLLM
from memspine.services.rerank.litellm_rerank import LiteLLMReranker

# ── embedding adapter ─────────────────────────────────────────────────────────


class _EmbedResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [{"embedding": v, "index": i} for i, v in enumerate(vectors)]


async def test_litellm_embedding_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    seen: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _EmbedResponse:
        seen.update(kwargs)
        return _EmbedResponse([[0.1, 0.2], [0.3, 0.4]])

    monkeypatch.setattr(litellm, "aembedding", fake_aembedding)
    emb = LiteLLMEmbedding("openai/text-embedding-3-small", dim=2, api_key="sk")
    out = await emb.embed(["a", "b"])
    assert out == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["model"] == "openai/text-embedding-3-small" and seen["input"] == ["a", "b"]
    assert emb.dim == 2 and emb.embedder_id == "litellm:openai/text-embedding-3-small"
    assert emb.manifest.quantization is None  # E4 off for cloud embedders


# ── rerank adapter: scores mapped back to input order ─────────────────────────


class _RerankResponse:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results


async def test_litellm_rerank_maps_scores_to_input_order(monkeypatch: pytest.MonkeyPatch) -> None:
    import litellm

    async def fake_arerank(**_kwargs: Any) -> _RerankResponse:
        # returned out of order, by descending relevance
        return _RerankResponse(
            [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
        )

    monkeypatch.setattr(litellm, "arerank", fake_arerank)
    reranker = LiteLLMReranker("cohere/rerank-english-v3.0")
    scores = await reranker.rerank("q", ["doc-a", "doc-b"])
    assert scores == [0.1, 0.9]  # realigned to input order
    assert await reranker.rerank("q", []) == []


# ── engine prefix routing ─────────────────────────────────────────────────────


async def test_engine_routes_litellm_model_prefix() -> None:
    eng = Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        llm={"roles": {"chat": {"model": "openai/gpt-4o"}}},
    )
    await eng.start()
    try:
        provider = eng._llm.for_role("chat")  # type: ignore[union-attr]
        assert isinstance(provider, LiteLLMLLM)
        assert provider.provider_id == "litellm:openai/gpt-4o"
    finally:
        await eng.stop()


async def test_engine_empty_model_raises() -> None:
    eng = Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        llm={"roles": {"chat": {"model": ""}}},
    )
    with pytest.raises(ConfigError, match=r"llm\.roles\.chat\.model is required"):
        await eng.start()
    await eng.stop()


async def test_litellm_embedder_requires_dim() -> None:
    eng = Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "litellm", "model": "openai/text-embedding-3-small"},
        memories={"semantic": {"enabled": True}},
    )
    with pytest.raises(ConfigError, match=r"embedding\.dim is required"):
        await eng.start()
    await eng.stop()


async def test_read_rerank_litellm_accepted_at_start() -> None:
    """A1 regression: read.rerank='litellm' (ADR-024) must pass the startup
    validator — it used to raise ConfigError because the validator tuple
    predated the litellm rerank branch. The reranker itself loads lazily."""
    eng = Engine(
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"semantic": {"enabled": True}},
        read={"rerank": "litellm", "rerank_model": "cohere/rerank-english-v3.0"},
    )
    await eng.start()  # must NOT raise
    try:
        assert eng.describe()["vector"] == "LanceDBVectorStore"  # engine came up
    finally:
        await eng.stop()
