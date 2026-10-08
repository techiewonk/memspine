"""N64: fastembed query instruction: queries only, documents unchanged, cache keyed by it."""

from __future__ import annotations

from typing import Any

import pytest

from memspine.config import constants
from memspine.config.schema import EmbeddingConfig
from memspine.services.cache.base import MemoryKV
from memspine.services.cache.semantic import CachedEmbedding
from memspine.services.embedding.base import embed_queries
from memspine.services.embedding.fastembed_local import FastembedEmbedding


class _FakeModel:
    def __init__(self) -> None:
        self.seen: list[str] = []

    def embed(self, texts: list[str]) -> Any:
        self.seen.extend(texts)
        return [[float(len(t)), 1.0] for t in texts]


def _emb(instruction: str | None) -> tuple[FastembedEmbedding, _FakeModel]:
    emb = FastembedEmbedding(query_instruction=instruction)
    model = _FakeModel()
    emb._model = model
    return emb, model


def test_off_by_default() -> None:
    assert EmbeddingConfig().query_instruction is None


async def test_no_instruction_embeds_queries_like_documents() -> None:
    emb, model = _emb(None)
    await embed_queries(emb, ["when did Ana go camping?"])
    assert model.seen == ["when did Ana go camping?"]
    assert emb.query_variant == ""


async def test_instruction_prefixes_queries_only() -> None:
    emb, model = _emb(constants.BGE_QUERY_INSTRUCTION)
    await emb.embed(["Ana went camping"])
    await embed_queries(emb, ["when did Ana go camping?"])
    assert model.seen == [
        "Ana went camping",
        constants.BGE_QUERY_INSTRUCTION + "when did Ana go camping?",
    ]
    assert emb.embedder_id == "fastembed:BAAI/bge-small-en-v1.5"  # identity unchanged


async def test_cache_never_serves_a_vector_made_under_the_other_setting() -> None:
    kv = MemoryKV()
    plain, _ = _emb(None)
    tuned, tuned_model = _emb(constants.BGE_QUERY_INSTRUCTION)
    first = await CachedEmbedding(plain, kv).embed_queries(["q?"])
    second = await CachedEmbedding(tuned, kv).embed_queries(["q?"])
    assert tuned_model.seen == [constants.BGE_QUERY_INSTRUCTION + "q?"]  # a miss, not a hit
    assert first != second


async def test_forget_drops_the_instruction_keyed_query_vector() -> None:
    kv = MemoryKV()
    tuned, model = _emb(constants.BGE_QUERY_INSTRUCTION)
    cached = CachedEmbedding(tuned, kv)
    await cached.embed_queries(["secret question"])
    await cached.forget("secret question")
    await cached.embed_queries(["secret question"])
    assert len(model.seen) == 2  # re-embedded: the cached query vector was erased


@pytest.mark.parametrize("value", ["", None])
def test_empty_instruction_is_off(value: str | None) -> None:
    emb, _ = _emb(value)
    assert emb.query_variant == ""
