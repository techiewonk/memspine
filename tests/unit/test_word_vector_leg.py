"""Word-vector leg (``read.word_vector_leg``): opt-in, fused by RRF, never a gate."""

from __future__ import annotations

from typing import Any

import pytest

from memspine import Engine
from memspine.engine import search_forensics
from memspine.services.embedding import word_vectors
from memspine.services.embedding.word_vectors import WordVectorEncoder


class FakeEncoder:
    """Puts "hobbies" and "kayaking" on one axis and everything else on another."""

    calls = 0

    def __init__(self, provider: str = "model2vec", model: str | None = None) -> None:
        self.provider, self.model_name = provider, model

    def encode(self, texts: list[str]) -> list[list[float]]:
        FakeEncoder.calls += 1
        return [[1.0, 0.0] if ("kayak" in t or "hobb" in t) else [0.0, 1.0] for t in texts]


def _engine(**read: Any) -> Engine:
    return Engine(
        template="core",
        dotenv_path=None,
        storage={"path": ":memory:"},
        embedding={"provider": "hash"},
        memories={"episodic": {"enabled": True}},
        read={"hybrid": False, "record_access": False, **read},
    )


async def _fill(eng: Engine) -> None:
    for text in [
        "Melanie went kayaking on the lake",
        "The bus was late again",
        "Pottery class today",
    ]:
        await eng.write(text, namespace="a", memory_type="episodic")


async def test_off_by_default_adds_no_leg() -> None:
    eng = _engine()
    await eng.start()
    try:
        await _fill(eng)
        with search_forensics() as stages:
            await eng.search("What are Melanie's hobbies?", namespace="a", top_k=3)
        assert [name for name, _ in stages.get("extra_legs", [])] == []
    finally:
        await eng.stop()


async def test_leg_ranks_related_word_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(word_vectors, "WordVectorEncoder", FakeEncoder)
    eng = _engine(word_vector_leg=True)
    await eng.start()
    try:
        await _fill(eng)
        with search_forensics() as stages:
            await eng.search("What are Melanie's hobbies?", namespace="a", top_k=3)
        legs = dict(stages["extra_legs"])
        assert "word_vector" in legs
        top_id = legs["word_vector"][0][0]
        record = await eng._require_started().get_record(top_id)
        assert "kayaking" in record.content
        calls = FakeEncoder.calls
        await eng.search("hobbies", namespace="a", top_k=3)
        assert FakeEncoder.calls == calls + 1  # record vectors cached: only the query is encoded
    finally:
        await eng.stop()


async def test_word_vector_top_k_caps_the_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(word_vectors, "WordVectorEncoder", FakeEncoder)
    eng = _engine(word_vector_leg=True, word_vector_top_k=1)
    await eng.start()
    try:
        await _fill(eng)
        with search_forensics() as stages:
            await eng.search("hobbies", namespace="a", top_k=3)
        assert len(dict(stages["extra_legs"])["word_vector"]) == 1
    finally:
        await eng.stop()


async def test_missing_provider_degrades_to_no_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    import builtins

    real_import = builtins.__import__

    def no_gensim(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "gensim":
            raise ImportError("no gensim")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_gensim)
    monkeypatch.setattr(word_vectors.importlib, "import_module", lambda n: no_gensim(n))
    eng = _engine(word_vector_leg=True, word_vector_provider="word2vec", word_vector_model="x.bin")
    await eng.start()
    try:
        await _fill(eng)
        results = await eng.search("hobbies", namespace="a", top_k=3)
        assert results  # retrieval still works
        assert eng._word_vectors_unavailable
    finally:
        await eng.stop()


def test_encoder_rejects_unknown_provider() -> None:
    with pytest.raises(ValueError):
        WordVectorEncoder("glove-magic")


def test_model2vec_encoder_relates_words() -> None:
    try:
        enc = WordVectorEncoder("model2vec", "minishlab/potion-retrieval-32M")
        q, a, b = enc.encode(
            ["What are her hobbies?", "She went kayaking", "The invoice is overdue"]
        )
    except Exception as exc:  # model not cached / offline
        pytest.skip(f"model2vec model unavailable: {exc}")
    dot = lambda x, y: sum(i * j for i, j in zip(x, y, strict=True))  # noqa: E731
    assert dot(q, a) > dot(q, b)
