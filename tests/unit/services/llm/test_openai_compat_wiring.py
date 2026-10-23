"""OpenAICompat embedding + rerank adapters and engine prefix-routing (Phase 4)."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from memspine import Engine
from memspine.exceptions import ConfigError
from memspine.services.embedding.openai_compat import OpenAICompatEmbedding
from memspine.services.llm.openai_compat import OpenAICompatLLM
from memspine.services.rerank.openai_compat import OpenAICompatReranker

# ── embedding adapter ─────────────────────────────────────────────────────────


class _EmbedResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.data = [{"embedding": v, "index": i} for i, v in enumerate(vectors)]


async def test_openai_compat_embedding_round_trips(monkeypatch: pytest.MonkeyPatch) -> None:
    from scripts.model_transport_stub import install_transport
    from scripts.model_transport_stub import transport_stub as openai_compat

    install_transport(monkeypatch)

    seen: dict[str, Any] = {}

    async def fake_aembedding(**kwargs: Any) -> _EmbedResponse:
        seen.update(kwargs)
        return _EmbedResponse([[0.1, 0.2], [0.3, 0.4]])

    monkeypatch.setattr(openai_compat, "aembedding", fake_aembedding)
    emb = OpenAICompatEmbedding(
        "openai/text-embedding-3-small", dim=2, api_key="sk", request_timeout_s=30
    )
    out = await emb.embed(["a", "b"])
    assert out == [[0.1, 0.2], [0.3, 0.4]]
    assert seen["model"] == "openai/text-embedding-3-small" and seen["input"] == ["a", "b"]
    assert emb.dim == 2 and emb.embedder_id.startswith(
        "openai_compatible:openai/text-embedding-3-small#"
    )
    assert emb.manifest.quantization is None  # E4 off for cloud embedders


# ── rerank adapter: scores mapped back to input order ─────────────────────────


class _RerankResponse:
    def __init__(self, results: list[dict[str, Any]]) -> None:
        self.results = results


async def test_openai_compat_rerank_maps_scores_to_input_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.model_transport_stub import install_transport
    from scripts.model_transport_stub import transport_stub as openai_compat

    install_transport(monkeypatch)

    async def fake_arerank(**_kwargs: Any) -> _RerankResponse:
        # returned out of order, by descending relevance
        return _RerankResponse(
            [{"index": 1, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.1}]
        )

    monkeypatch.setattr(openai_compat, "arerank", fake_arerank)
    reranker = OpenAICompatReranker("cohere/rerank-english-v3.0", request_timeout_s=30)
    scores = await reranker.rerank("q", ["doc-a", "doc-b"])
    assert scores == [0.1, 0.9]  # realigned to input order
    assert await reranker.rerank("q", []) == []


# ── engine prefix routing ─────────────────────────────────────────────────────


async def test_engine_routes_openai_compat_model_prefix() -> None:
    eng = Engine(
        dotenv_path=None,
        deployment={"id": "unit-test"},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "chat": {
                    "provider": "openai_compatible",
                    "timeout_s": 30.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": "openai/gpt-4o"},
                }
            },
        },
    )
    await eng.start()
    try:
        provider = eng._llm.provider("chat")  # type: ignore[union-attr]
        assert isinstance(provider, OpenAICompatLLM)
        assert provider.provider_id == "openai_compatible:openai/gpt-4o"
    finally:
        await eng.stop()


async def test_engine_empty_model_raises() -> None:
    eng = Engine(
        dotenv_path=None,
        deployment={"id": "unit-test"},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "llm": {
                "chat": {
                    "provider": "openai_compatible",
                    "timeout_s": 30.0,
                    "settings": {"api_base": "http://test.invalid/v1", "model": ""},
                }
            },
        },
    )
    with pytest.raises(ConfigError, match=r"invalid settings for llm\.openai_compat.*model"):
        await eng.start()
    await eng.stop()


async def test_openai_compat_embedder_requires_dim() -> None:
    from memspine.config.schema.providers import OpenAICompatEmbeddingSettings

    known = OpenAICompatEmbeddingSettings(request_timeout_s=30.0)
    assert known.dim == 1536
    with pytest.raises(ValidationError, match=r"settings\.dim is required"):
        OpenAICompatEmbeddingSettings(
            model="custom/embedding-model-with-unknown-dimension",
            request_timeout_s=30.0,
        )

    eng = Engine(
        dotenv_path=None,
        deployment={"id": "unit-test"},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {
                "provider": "openai_compatible",
                "settings": {
                    "api_base": "http://test.invalid/v1",
                    "model": "custom/embedding-model-with-unknown-dimension",
                    "request_timeout_s": 30.0,
                },
            }
        },
        memories={"semantic": {"enabled": True}},
    )
    with pytest.raises(ConfigError, match=r"invalid settings for embedding\.openai_compat"):
        await eng.start()
    await eng.stop()


async def test_read_rerank_openai_compat_accepted_at_start() -> None:
    """A1 regression: a openai_compat model.rerank.chain entry (ADR-024) must pass the startup
    validator — it used to raise ConfigError because the validator tuple
    predated the openai_compat rerank branch. The reranker itself loads lazily."""
    eng = Engine(
        dotenv_path=None,
        deployment={"id": "unit-test"},
        storage={
            "records": {
                "provider": "sqlite",
                "settings": {"path": ":memory:", "busy_timeout_s": 5.0},
            }
        },
        model={
            "embedding": {"provider": "hash", "settings": {}},
            "rerank": {
                "chain": [
                    {
                        "provider": "openai_compatible",
                        "settings": {
                            "api_base": "http://test.invalid/v1",
                            "model": "cohere/rerank-english-v3.0",
                            "request_timeout_s": 30.0,
                        },
                    }
                ]
            },
        },
        memories={"semantic": {"enabled": True}},
    )
    await eng.start()  # must NOT raise
    try:
        assert eng.describe()["vector"] == "LanceDBVectorStore"  # engine came up
    finally:
        await eng.stop()
