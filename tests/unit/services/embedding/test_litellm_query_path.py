"""LiteLLM embedder: requested dimensions and asymmetric query/document types."""

from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from memspine.services.cache.semantic import CachedEmbedding
from memspine.services.embedding.base import embed_queries
from memspine.services.embedding.litellm_embed import LiteLLMEmbedding


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []

    async def aembedding(**kwargs: Any) -> Any:
        seen.append(kwargs)
        tag = 1.0 if kwargs.get("input_type") == "search_query" else 0.0
        data = [{"embedding": [tag] * 4} for _ in kwargs["input"]]
        return types.SimpleNamespace(data=data)

    monkeypatch.setitem(sys.modules, "litellm", types.SimpleNamespace(aembedding=aembedding))
    return seen


async def test_defaults_send_no_new_parameters(calls: list[dict[str, Any]]) -> None:
    emb = LiteLLMEmbedding("bedrock/cohere.embed-v4:0", 4)
    await emb.embed(["doc"])
    await embed_queries(emb, ["q"])
    assert all("dimensions" not in c and "input_type" not in c for c in calls)
    assert emb.embedder_id == "litellm:bedrock/cohere.embed-v4:0"  # identity unchanged


async def test_requested_dims_and_input_types(calls: list[dict[str, Any]]) -> None:
    emb = LiteLLMEmbedding(
        "bedrock/cohere.embed-v4:0",
        4,
        request_dimensions=True,
        query_input_type="search_query",
        document_input_type="search_document",
    )
    await emb.embed(["doc"])
    await embed_queries(emb, ["q"])
    assert calls[0]["dimensions"] == 4 and calls[0]["input_type"] == "search_document"
    assert calls[1]["input_type"] == "search_query"
    assert "@4" in emb.embedder_id and "search_query" in emb.embedder_id


async def test_cache_keeps_query_and_document_vectors_apart(calls: list[dict[str, Any]]) -> None:
    from memspine.services.cache.base import MemoryKV  # in-process KV

    emb = LiteLLMEmbedding(
        "bedrock/cohere.embed-v4:0",
        4,
        query_input_type="search_query",
        document_input_type="search_document",
    )
    cached = CachedEmbedding(emb, MemoryKV())
    [doc] = await cached.embed(["same text"])
    [query] = await embed_queries(cached, ["same text"])
    assert doc != query  # a cached document vector is never served for a query
    [query_again] = await embed_queries(cached, ["same text"])
    assert query_again == query and len(calls) == 2  # second query hit the cache
